"""NEURAL SCROLL — a custom desktop control deck for the scroll macro.

The UI uses only tkinter. Global input capture and mouse output are provided by
pynput, which is intentionally imported without installing packages at runtime.
"""

from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

try:
    from pynput import keyboard as _keyboard
    from pynput.mouse import Button as _Button
    from pynput.mouse import Controller as _MouseController
    from pynput.mouse import Listener as _MouseListener

    keyboard: Any = _keyboard
    Button: Any = _Button
    MouseController: Any = _MouseController
    MouseListener: Any = _MouseListener
    PYNPUT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001 - pynput backend initialization varies by OS.
    keyboard = None
    Button = None
    MouseController = None
    MouseListener = None
    PYNPUT_ERROR = exc


# --- Theme -----------------------------------------------------------------
BG = "#05070D"
SIDEBAR = "#080B13"
PANEL = "#0B101B"
PANEL_ALT = "#0E1523"
BORDER = "#1B2940"
GRID = "#142035"
TEXT = "#EAF2FF"
MUTED = "#72839F"
CYAN = "#43F4C7"
CYAN_DARK = "#123D38"
PURPLE = "#9B7BFF"
RED = "#FF5574"
AMBER = "#FFCC66"
FONT = "Segoe UI"
MONO = "Consolas"

BINDING_OPTIONS = (
    "Keyboard · F",
    "Keyboard · G",
    "Keyboard · Space",
    "Keyboard · Shift",
    "Keyboard · Ctrl",
    "Mouse · Middle",
    "Mouse · Button 4",
    "Mouse · Button 5",
)


def rounded_rect(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    **kwargs: Any,
) -> int:
    """Draw a smooth rounded rectangle and return its canvas item id."""
    radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = (
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    )
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


@dataclass(frozen=True)
class MacroConfig:
    binding: str = BINDING_OPTIONS[0]
    mode: str = "toggle"
    cadence_ms: int = 10
    pulse_delay_ms: int = 4
    intensity: int = 1


class MacroEngine:
    """Thread-safe input listeners and cancellable scroll-pulse worker."""

    def __init__(self, event_sink: Callable[[str, dict[str, Any]], None]) -> None:
        self._event_sink = event_sink
        self._config = MacroConfig()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._enabled = False
        self._online = False
        self._pressed_keys: set[str] = set()
        self._pulse_count = 0
        self._mouse: Any = None
        self._keyboard_listener: Any = None
        self._mouse_listener: Any = None
        self._worker: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def configure(self, *, disarm: bool = False, **changes: Any) -> None:
        with self._lock:
            if disarm:
                self._transition_locked(False, source="reconfigure")
            if "binding" in changes or "mode" in changes:
                self._pressed_keys.clear()
            self._config = replace(self._config, **changes)
            config = self._config
            self._emit("config", config=config)
        self._wake_event.set()

    def start(self) -> None:
        if PYNPUT_ERROR is not None:
            raise RuntimeError(f"pynput backend unavailable: {PYNPUT_ERROR}")
        with self._lock:
            if self._online:
                return
            self._stop_event.clear()
            mouse = MouseController()
            keyboard_listener = keyboard.Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
            )
            mouse_listener = MouseListener(on_click=self._on_mouse_click)

        started_listeners: list[Any] = []
        try:
            for listener in (keyboard_listener, mouse_listener):
                listener.start()
                started_listeners.append(listener)
                listener.wait()
        except Exception:
            for listener in started_listeners:
                try:
                    listener.stop()
                    listener.join(timeout=0.5)
                except Exception:  # noqa: BLE001, S110 - best-effort startup rollback.
                    pass
            raise

        with self._lock:
            self._mouse = mouse
            self._keyboard_listener = keyboard_listener
            self._mouse_listener = mouse_listener
            self._online = True
            self._worker = threading.Thread(
                target=self._run,
                name="macro-pulse-worker",
                daemon=True,
            )
            self._worker.start()
            # Emit while holding the state lock so later callback events cannot
            # overtake the online event in the UI queue.
            self._emit("online")

    def stop(self) -> None:
        with self._lock:
            if not self._online and self._worker is None:
                return
            self._online = False
            self._pressed_keys.clear()
            self._transition_locked(False, source="shutdown")
            listeners = (self._keyboard_listener, self._mouse_listener)
            worker = self._worker
            self._keyboard_listener = None
            self._mouse_listener = None
            self._worker = None
        self._stop_event.set()
        self._wake_event.set()
        for listener in listeners:
            if listener is not None:
                try:
                    listener.stop()
                    if listener is not threading.current_thread():
                        listener.join(timeout=1.0)
                except Exception as exc:  # noqa: BLE001 - third-party listener backend.
                    self._emit("error", message=f"Listener shutdown warning: {exc}")
        if worker and worker is not threading.current_thread():
            worker.join(timeout=1.0)
        self._emit("offline")

    def _transition_locked(self, enabled: bool, source: str) -> bool:
        """Apply one ordered state transition; caller must hold ``_lock``."""
        if enabled and not self._online:
            return False
        if self._enabled == enabled:
            return False
        self._enabled = enabled
        self._wake_event.set()
        self._emit("state", enabled=enabled, source=source)
        return True

    def toggle(self, source: str = "ui") -> None:
        with self._lock:
            self._transition_locked(not self._enabled, source=source)

    def set_enabled(self, enabled: bool, source: str = "ui") -> None:
        with self._lock:
            self._transition_locked(enabled, source=source)

    def _emit(self, kind: str, **payload: Any) -> None:
        self._event_sink(kind, payload)

    def _binding_matches_key(self, key: Any, binding: str) -> bool:
        if not binding.startswith("Keyboard ·"):
            return False
        name = binding.split("·", 1)[1].strip()
        special_keys = {
            "Space": keyboard.Key.space,
            "Shift": keyboard.Key.shift,
            "Ctrl": keyboard.Key.ctrl,
        }
        if name in special_keys:
            if key == special_keys[name]:
                return True
            # Left/right modifier variants differ on some backends.
            return name == "Shift" and key in (keyboard.Key.shift_l, keyboard.Key.shift_r) or (
                name == "Ctrl" and key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
            )
        try:
            return key == keyboard.KeyCode.from_char(name.lower())
        except (AttributeError, ValueError):
            return False

    def _binding_matches_button(self, button: Any, binding: str) -> bool:
        if not binding.startswith("Mouse ·"):
            return False
        name = binding.split("·", 1)[1].strip()
        buttons = {
            "Middle": Button.middle,
            "Button 4": Button.x1,
            "Button 5": Button.x2,
        }
        return button == buttons.get(name)

    def _on_key_press(self, key: Any) -> None:
        with self._lock:
            if not self._online:
                return
            config = self._config
            identity = str(key)
            if not self._binding_matches_key(key, config.binding):
                return
            if identity in self._pressed_keys:
                return  # Suppress OS key-repeat from re-toggling.
            self._pressed_keys.add(identity)
            next_state = not self._enabled if config.mode == "toggle" else True
            self._transition_locked(next_state, source=config.binding)

    def _on_key_release(self, key: Any) -> None:
        with self._lock:
            if not self._online:
                return
            config = self._config
            identity = str(key)
            self._pressed_keys.discard(identity)
            matches = self._binding_matches_key(key, config.binding)
            if matches and config.mode == "hold":
                # Left/right modifiers are separate keys; remain armed until all
                # matching keys have been released.
                self._transition_locked(bool(self._pressed_keys), source=config.binding)

    def _on_mouse_click(self, _x: int, _y: int, button: Any, pressed: bool) -> None:
        with self._lock:
            if not self._online:
                return
            config = self._config
            if not self._binding_matches_button(button, config.binding):
                return
            if config.mode == "toggle":
                if not pressed:
                    return
                next_state = not self._enabled
            else:
                next_state = pressed
            self._transition_locked(next_state, source=config.binding)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self.enabled:
                self._wake_event.wait(0.1)
                self._wake_event.clear()
                continue
            with self._lock:
                config = self._config
            completed = True
            net_delta = 0
            # Preserve the original neutral/up/down/neutral pulse sequence. If
            # cancellation arrives after the upward event, always send the
            # compensating downward event before exiting the pulse.
            for planned_delta in (0, config.intensity, -config.intensity, 0):
                cancelled = self._stop_event.is_set() or not self.enabled
                if cancelled and net_delta == 0:
                    completed = False
                    break
                delta = -net_delta if cancelled else planned_delta
                if delta:
                    try:
                        self._mouse.scroll(0, delta)
                        net_delta += delta
                    except Exception as exc:  # noqa: BLE001 - OS mouse backend failure.
                        self.set_enabled(False, source="driver-error")
                        self._emit("error", message=f"Mouse output failed: {exc}")
                        completed = False
                        break
                if cancelled and net_delta == 0:
                    completed = False
                    break
                self._stop_event.wait(config.pulse_delay_ms / 1000)
            if completed:
                self._pulse_count += 1
                self._emit("pulse", count=self._pulse_count)
            self._stop_event.wait(config.cadence_ms / 1000)


class Panel(tk.Frame):
    def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
        super().__init__(parent, bg=BORDER, **kwargs)
        self.body = tk.Frame(self, bg=PANEL)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)


class NeonButton(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        width: int = 180,
        height: int = 44,
        accent: str = CYAN,
        filled: bool = False,
        font_size: int = 10,
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=PANEL,
            highlightthickness=0,
            cursor="hand2",
        )
        self._text = text
        self._command = command
        self._accent = accent
        self._filled = filled
        self._hovered = False
        self._font_size = font_size
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self.bind("<Button-1>", lambda _event: self._command())
        self._draw()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._draw()

    def set_state(self, *, text: str | None = None, accent: str | None = None, filled: bool | None = None) -> None:
        if text is not None:
            self._text = text
        if accent is not None:
            self._accent = accent
        if filled is not None:
            self._filled = filled
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = int(self["width"])
        height = int(self["height"])
        if self._filled:
            fill = self._accent if not self._hovered else TEXT
            foreground = BG
        else:
            fill = PANEL_ALT if self._hovered else PANEL
            foreground = self._accent if not self._hovered else TEXT
        rounded_rect(self, 2, 2, width - 2, height - 2, 10, fill=fill, outline=self._accent, width=1)
        self.create_text(
            width / 2,
            height / 2,
            text=self._text,
            fill=foreground,
            font=(FONT, self._font_size, "bold"),
        )


class StatusReactor(tk.Canvas):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=PANEL, highlightthickness=0, width=360, height=340)
        self.active = False
        self.phase = 0.0
        self._draw()
        self.after(40, self._animate)

    def set_active(self, active: bool) -> None:
        self.active = active

    def _animate(self) -> None:
        if not self.winfo_exists():
            return
        self.phase = (self.phase + (0.10 if self.active else 0.025)) % (math.pi * 2)
        self._draw()
        self.after(40, self._animate)

    def _draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 360)
        height = max(self.winfo_height(), 340)
        cx, cy = width / 2, height / 2 - 5
        accent = CYAN if self.active else MUTED

        for radius in (134, 112, 90):
            self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, outline=GRID, width=1)
        for angle in range(0, 360, 30):
            radians = math.radians(angle)
            inner, outer = 119, 129
            self.create_line(
                cx + math.cos(radians) * inner,
                cy + math.sin(radians) * inner,
                cx + math.cos(radians) * outer,
                cy + math.sin(radians) * outer,
                fill=accent if angle % 90 == 0 else GRID,
                width=2 if angle % 90 == 0 else 1,
            )

        sweep = math.degrees(self.phase)
        self.create_arc(cx - 124, cy - 124, cx + 124, cy + 124, start=sweep, extent=72, style="arc", outline=accent, width=3)
        self.create_arc(cx - 102, cy - 102, cx + 102, cy + 102, start=-sweep, extent=105, style="arc", outline=PURPLE, width=2)

        glow_radius = 74 + (math.sin(self.phase * 2) * 4 if self.active else 0)
        self.create_oval(
            cx - glow_radius,
            cy - glow_radius,
            cx + glow_radius,
            cy + glow_radius,
            fill=CYAN_DARK if self.active else "#111824",
            outline=accent,
            width=2,
        )
        self.create_oval(cx - 57, cy - 57, cx + 57, cy + 57, fill=BG, outline=GRID, width=1)

        for index in range(4):
            angle = self.phase + index * math.pi / 2
            orbit = 112
            px = cx + math.cos(angle) * orbit
            py = cy + math.sin(angle) * orbit
            size = 4 if self.active else 2
            self.create_oval(px - size, py - size, px + size, py + size, fill=accent, outline="")

        self.create_text(cx, cy - 18, text="●", fill=accent, font=(FONT, 17))
        self.create_text(
            cx,
            cy + 13,
            text="ARMED" if self.active else "STANDBY",
            fill=TEXT,
            font=(MONO, 17, "bold"),
        )
        self.create_text(
            cx,
            cy + 40,
            text="PULSE STREAM ACTIVE" if self.active else "AWAITING ACTIVATION",
            fill=accent,
            font=(MONO, 8, "bold"),
        )


class ActivityGraph(tk.Canvas):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=PANEL, highlightthickness=0, height=118)
        self.values: deque[float] = deque([0.0] * 64, maxlen=64)
        self.pending_pulses = 0
        self.active = False
        self.after(100, self._tick)

    def register_pulse(self) -> None:
        self.pending_pulses += 1

    def set_active(self, active: bool) -> None:
        self.active = active

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        value = min(1.0, self.pending_pulses / 5) if self.active else 0.02
        self.pending_pulses = 0
        self.values.append(value)
        self._draw()
        self.after(100, self._tick)

    def _draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 400)
        height = max(self.winfo_height(), 118)
        for x in range(0, width, 52):
            self.create_line(x, 0, x, height, fill=GRID)
        for y in range(10, height, 24):
            self.create_line(0, y, width, y, fill=GRID)
        values = list(self.values)
        step = width / (len(values) - 1)
        points: list[float] = []
        for index, value in enumerate(values):
            jitter = math.sin(index * 1.7) * 0.08 if value > 0.1 else 0
            x = index * step
            y = height - 12 - max(0, min(1, value + jitter)) * (height - 30)
            points.extend((x, y))
        if len(points) >= 4:
            self.create_line(*points, fill=CYAN if self.active else MUTED, width=2, smooth=True)
        self.create_text(10, 10, anchor="nw", text="LIVE PULSE TELEMETRY", fill=MUTED, font=(MONO, 8, "bold"))
        self.create_text(
            width - 10,
            10,
            anchor="ne",
            text="STREAMING" if self.active else "IDLE",
            fill=CYAN if self.active else MUTED,
            font=(MONO, 8, "bold"),
        )


class MacroApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Neural Scroll // Command Deck")
        self.root.geometry("1180x760")
        self.root.minsize(1080, 700)
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.events: queue.SimpleQueue[tuple[str, dict[str, Any]]] = queue.SimpleQueue()
        self.engine = MacroEngine(self._queue_event)
        self.binding_var = tk.StringVar(value=BINDING_OPTIONS[0])
        self.mode = "toggle"
        self.cadence_var = tk.IntVar(value=10)
        self.intensity_var = tk.IntVar(value=1)
        self.started_at = time.monotonic()
        self._drag_origin = (0, 0)
        self._online = False
        self._closing = False
        self._last_pulse_count = 0

        self._build_window()
        self._log("BOOT", "Control deck initialized")
        self._log("SAFE", "Macro starts disarmed")
        self.root.after(100, self._start_engine)
        self.root.after(50, self._process_events)
        self.root.after(1000, self._update_uptime)

    def _build_window(self) -> None:
        shell = tk.Frame(self.root, bg=BORDER)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        self._build_titlebar(shell)

        body = tk.Frame(shell, bg=BG)
        body.pack(fill="both", expand=True)
        self._build_sidebar(body)

        content = tk.Frame(body, bg=BG)
        content.pack(side="left", fill="both", expand=True, padx=24, pady=(20, 18))

        header = tk.Frame(content, bg=BG)
        header.pack(fill="x", pady=(0, 16))
        title_box = tk.Frame(header, bg=BG)
        title_box.pack(side="left")
        tk.Label(title_box, text="NEURAL INPUT", bg=BG, fg=TEXT, font=(FONT, 23, "bold")).pack(anchor="w")
        tk.Label(
            title_box,
            text="/ PRECISION SCROLL COMMAND DECK",
            bg=BG,
            fg=CYAN,
            font=(MONO, 9, "bold"),
        ).pack(anchor="w", pady=(2, 0))

        status_box = tk.Frame(header, bg=BG)
        status_box.pack(side="right", anchor="n", pady=4)
        self.driver_badge = tk.Label(
            status_box,
            text="  ● DRIVER BOOTING  ",
            bg=PANEL_ALT,
            fg=AMBER,
            font=(MONO, 8, "bold"),
            padx=8,
            pady=7,
        )
        self.driver_badge.pack(side="left", padx=(0, 8))
        self.uptime_label = tk.Label(
            status_box,
            text="UP 00:00:00",
            bg=PANEL_ALT,
            fg=MUTED,
            font=(MONO, 8, "bold"),
            padx=10,
            pady=7,
        )
        self.uptime_label.pack(side="left")

        upper = tk.Frame(content, bg=BG)
        upper.pack(fill="both", expand=True)

        hero_panel = Panel(upper, width=390)
        hero_panel.pack(side="left", fill="both", padx=(0, 14))
        hero_panel.pack_propagate(False)
        self.reactor = StatusReactor(hero_panel.body)
        self.reactor.pack(fill="both", expand=True, padx=10, pady=(5, 0))
        self.arm_button = NeonButton(
            hero_panel.body,
            text="ARM SYSTEM",
            command=self._toggle_from_ui,
            width=330,
            height=48,
            accent=CYAN,
            filled=True,
            font_size=11,
        )
        self.arm_button.pack(pady=(0, 12))

        controls_panel = Panel(upper)
        controls_panel.pack(side="left", fill="both", expand=True)
        controls = controls_panel.body
        controls.grid_columnconfigure(0, weight=1)
        controls.grid_columnconfigure(1, weight=1)

        self._section_title(controls, "01", "ACTIVATION LINK", 0)
        bind_frame = tk.Frame(controls, bg=PANEL_ALT, highlightbackground=BORDER, highlightthickness=1)
        bind_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 15))
        tk.Label(bind_frame, text="TRIGGER", bg=PANEL_ALT, fg=MUTED, font=(MONO, 8, "bold")).pack(
            side="left", padx=13
        )
        option = tk.OptionMenu(
            bind_frame,
            self.binding_var,
            *BINDING_OPTIONS,
            command=lambda value: self._on_binding_change(str(value)),
        )
        option.configure(
            bg=PANEL_ALT,
            fg=TEXT,
            activebackground=PANEL_ALT,
            activeforeground=CYAN,
            highlightthickness=0,
            bd=0,
            relief="flat",
            font=(MONO, 10, "bold"),
            cursor="hand2",
            width=20,
        )
        option["menu"].configure(bg=PANEL_ALT, fg=TEXT, activebackground=CYAN_DARK, activeforeground=TEXT, bd=0)
        option.pack(side="right", padx=8, pady=6)

        self._section_title(controls, "02", "RESPONSE MODE", 2)
        mode_frame = tk.Frame(controls, bg=PANEL)
        mode_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 15))
        self.toggle_button = NeonButton(
            mode_frame, "TOGGLE", lambda: self._set_mode("toggle"), width=190, height=38, accent=CYAN, filled=True
        )
        self.toggle_button.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.hold_button = NeonButton(
            mode_frame, "HOLD", lambda: self._set_mode("hold"), width=190, height=38, accent=PURPLE
        )
        self.hold_button.pack(side="left", fill="x", expand=True, padx=(5, 0))

        self._section_title(controls, "03", "PULSE DYNAMICS", 4)
        self.cadence_value = self._make_slider(
            controls,
            row=5,
            label="CADENCE",
            variable=self.cadence_var,
            from_=8,
            to=40,
            suffix="ms",
            command=self._on_cadence_change,
        )
        self.intensity_value = self._make_slider(
            controls,
            row=6,
            label="INTENSITY",
            variable=self.intensity_var,
            from_=1,
            to=5,
            suffix="x",
            command=self._on_intensity_change,
        )

        warning = tk.Frame(controls, bg="#17151D", highlightbackground="#403044", highlightthickness=1)
        warning.grid(row=7, column=0, columnspan=2, sticky="ew", padx=18, pady=(12, 16))
        tk.Label(warning, text="!", bg="#17151D", fg=AMBER, font=(MONO, 13, "bold")).pack(side="left", padx=(12, 8))
        tk.Label(
            warning,
            text="Global input is active system-wide. Use responsibly.",
            bg="#17151D",
            fg="#C8B78D",
            font=(FONT, 9),
        ).pack(side="left", pady=10)

        lower = tk.Frame(content, bg=BG, height=160)
        lower.pack(fill="x", pady=(14, 0))
        lower.pack_propagate(False)

        graph_panel = Panel(lower)
        graph_panel.pack(side="left", fill="both", expand=True, padx=(0, 14))
        self.graph = ActivityGraph(graph_panel.body)
        self.graph.pack(fill="both", expand=True, padx=10, pady=8)

        log_panel = Panel(lower, width=350)
        log_panel.pack(side="left", fill="both")
        log_panel.pack_propagate(False)
        log_header = tk.Frame(log_panel.body, bg=PANEL)
        log_header.pack(fill="x", padx=12, pady=(10, 3))
        tk.Label(log_header, text="EVENT STREAM", bg=PANEL, fg=MUTED, font=(MONO, 8, "bold")).pack(side="left")
        tk.Label(log_header, text="● LIVE", bg=PANEL, fg=CYAN, font=(MONO, 8, "bold")).pack(side="right")
        self.log_text = tk.Text(
            log_panel.body,
            bg=PANEL,
            fg="#A8B6CC",
            insertbackground=CYAN,
            bd=0,
            highlightthickness=0,
            font=(MONO, 8),
            height=6,
            padx=12,
            pady=2,
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("time", foreground=MUTED)
        self.log_text.tag_configure("good", foreground=CYAN)
        self.log_text.tag_configure("alert", foreground=RED)

    def _build_titlebar(self, parent: tk.Misc) -> None:
        bar = tk.Frame(parent, bg=SIDEBAR, height=42)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        mark = tk.Label(bar, text="  ◈  NSC / 4.2", bg=SIDEBAR, fg=CYAN, font=(MONO, 9, "bold"))
        mark.pack(side="left", padx=10)
        center = tk.Label(
            bar,
            text="ENCRYPTED CONTROL CHANNEL  //  LOCALHOST",
            bg=SIDEBAR,
            fg="#3F516C",
            font=(MONO, 8),
        )
        center.pack(side="left", expand=True)
        minimize = tk.Label(bar, text="—", bg=SIDEBAR, fg=MUTED, width=5, font=(FONT, 11), cursor="hand2")
        minimize.pack(side="right", fill="y")
        minimize.bind("<Button-1>", lambda _event: self._minimize())
        close = tk.Label(bar, text="×", bg=SIDEBAR, fg=MUTED, width=5, font=(FONT, 15), cursor="hand2")
        close.pack(side="right", fill="y")
        close.bind("<Enter>", lambda _event: close.configure(bg=RED, fg=TEXT))
        close.bind("<Leave>", lambda _event: close.configure(bg=SIDEBAR, fg=MUTED))
        close.bind("<Button-1>", lambda _event: self.close())
        for widget in (bar, mark, center):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag_window)

    def _build_sidebar(self, parent: tk.Misc) -> None:
        sidebar = tk.Frame(parent, bg=SIDEBAR, width=195)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Canvas(sidebar, bg=SIDEBAR, width=195, height=120, highlightthickness=0)
        brand.pack(fill="x")
        brand.create_polygon(26, 28, 52, 16, 78, 28, 78, 56, 52, 69, 26, 56, fill="", outline=CYAN, width=2)
        brand.create_text(52, 43, text="N", fill=TEXT, font=(MONO, 17, "bold"))
        brand.create_text(94, 31, anchor="w", text="NEURAL", fill=TEXT, font=(FONT, 13, "bold"))
        brand.create_text(94, 51, anchor="w", text="SCROLL", fill=CYAN, font=(MONO, 9, "bold"))
        brand.create_text(27, 94, anchor="w", text="COMMAND MODULE", fill=MUTED, font=(MONO, 7))

        self._nav_item(sidebar, "01", "CONTROL DECK", active=True)
        self._nav_item(sidebar, "02", "INPUT MATRIX")
        self._nav_item(sidebar, "03", "TELEMETRY")

        tk.Frame(sidebar, bg=BORDER, height=1).pack(fill="x", padx=22, pady=20)
        tk.Label(sidebar, text="CORE STATUS", bg=SIDEBAR, fg="#465975", font=(MONO, 7, "bold")).pack(
            anchor="w", padx=25
        )
        self.side_status = tk.Label(
            sidebar,
            text="●  INITIALIZING",
            bg=SIDEBAR,
            fg=AMBER,
            font=(MONO, 8, "bold"),
        )
        self.side_status.pack(anchor="w", padx=25, pady=(10, 4))
        tk.Label(sidebar, text="THREAD SAFE", bg=SIDEBAR, fg=MUTED, font=(MONO, 7)).pack(anchor="w", padx=25)

        footer = tk.Frame(sidebar, bg=SIDEBAR)
        footer.pack(side="bottom", fill="x", padx=24, pady=22)
        tk.Label(footer, text="SOURCE NODE", bg=SIDEBAR, fg="#465975", font=(MONO, 7, "bold")).pack(anchor="w")
        tk.Label(footer, text="@FWAKAAZZ", bg=SIDEBAR, fg=TEXT, font=(MONO, 8, "bold")).pack(anchor="w", pady=(5, 1))
        tk.Label(footer, text="BUILD 2026.08", bg=SIDEBAR, fg=MUTED, font=(MONO, 7)).pack(anchor="w")

    def _nav_item(self, parent: tk.Misc, number: str, text: str, active: bool = False) -> None:
        row = tk.Frame(parent, bg=PANEL_ALT if active else SIDEBAR, height=44)
        row.pack(fill="x", padx=10, pady=2)
        row.pack_propagate(False)
        if active:
            tk.Frame(row, bg=CYAN, width=3).pack(side="left", fill="y")
        tk.Label(row, text=number, bg=row["bg"], fg=CYAN if active else "#42516A", font=(MONO, 8)).pack(
            side="left", padx=(12, 10)
        )
        tk.Label(
            row,
            text=text,
            bg=row["bg"],
            fg=TEXT if active else MUTED,
            font=(MONO, 8, "bold"),
        ).pack(side="left")

    def _section_title(self, parent: tk.Misc, number: str, title: str, row: int) -> None:
        frame = tk.Frame(parent, bg=PANEL)
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", padx=18, pady=(15, 9))
        tk.Label(frame, text=number, bg=PANEL, fg=CYAN, font=(MONO, 8, "bold")).pack(side="left")
        tk.Label(frame, text=f" / {title}", bg=PANEL, fg=TEXT, font=(MONO, 9, "bold")).pack(side="left")

    def _make_slider(
        self,
        parent: tk.Misc,
        row: int,
        label: str,
        variable: tk.IntVar,
        from_: int,
        to: int,
        suffix: str,
        command: Callable[[str], None],
    ) -> tk.Label:
        frame = tk.Frame(parent, bg=PANEL)
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", padx=18, pady=4)
        header = tk.Frame(frame, bg=PANEL)
        header.pack(fill="x")
        tk.Label(header, text=label, bg=PANEL, fg=MUTED, font=(MONO, 8, "bold")).pack(side="left")
        value_label = tk.Label(
            header,
            text=f"{variable.get()}{suffix}",
            bg=PANEL,
            fg=CYAN,
            font=(MONO, 9, "bold"),
        )
        value_label.pack(side="right")
        scale = tk.Scale(
            frame,
            variable=variable,
            from_=from_,
            to=to,
            orient="horizontal",
            command=command,
            showvalue=False,
            bg=PANEL,
            fg=TEXT,
            troughcolor=GRID,
            activebackground=TEXT,
            highlightthickness=0,
            bd=0,
            relief="flat",
            sliderrelief="flat",
            sliderlength=16,
            width=8,
        )
        scale.pack(fill="x", pady=(2, 0))
        return value_label

    def _queue_event(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.put((kind, payload))

    def _start_engine(self) -> None:
        try:
            self.engine.start()
        except Exception as exc:  # noqa: BLE001 - surface any platform/backend startup failure.
            self._online = False
            self.driver_badge.configure(text="  ● DRIVER ERROR  ", fg=RED)
            self.side_status.configure(text="●  DRIVER OFFLINE", fg=RED)
            self._log("ERROR", str(exc), alert=True)

    def _process_events(self) -> None:
        if self._closing:
            return
        processed = 0
        while processed < 100:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if kind == "online":
                self._online = True
                self.driver_badge.configure(text="  ● DRIVER ONLINE  ", fg=CYAN)
                self.side_status.configure(text="●  CORE ONLINE", fg=CYAN)
                self._log("CORE", "Global listeners connected")
            elif kind == "offline":
                self._online = False
            elif kind == "state":
                self._render_state(bool(payload["enabled"]))
                source = payload.get("source", "unknown")
                self._log("STATE", f"{'ARMED' if payload['enabled'] else 'DISARMED'} via {source}")
            elif kind == "pulse":
                self.graph.register_pulse()
                self._last_pulse_count = int(payload["count"])
            elif kind == "error":
                self._log("ERROR", str(payload["message"]), alert=True)
        self.root.after(50, self._process_events)

    def _render_state(self, enabled: bool) -> None:
        self.reactor.set_active(enabled)
        self.graph.set_active(enabled)
        if enabled:
            self.arm_button.set_state(text="FORCE DISARM", accent=RED, filled=False)
        elif self.mode == "hold":
            self.arm_button.set_state(text="HOLD TRIGGER TO ARM", accent=PURPLE, filled=False)
        else:
            self.arm_button.set_state(text="ARM SYSTEM", accent=CYAN, filled=True)

    def _toggle_from_ui(self) -> None:
        if not self._online:
            self._log("BLOCK", "Driver is offline; install/check pynput", alert=True)
            return
        if self.mode == "hold":
            if self.engine.enabled:
                self.engine.set_enabled(False, source="control-deck")
            else:
                self._log("HOLD", f"Hold {self.binding_var.get()} to arm")
            return
        self.engine.toggle(source="control-deck")

    def _on_binding_change(self, value: str) -> None:
        self.engine.configure(binding=value, disarm=True)
        self._log("LINK", f"Trigger mapped to {value}")

    def _set_mode(self, mode: str) -> None:
        if self.mode == mode:
            return
        self.mode = mode
        self.engine.configure(mode=mode, disarm=True)
        self.toggle_button.set_state(filled=mode == "toggle", accent=CYAN)
        self.hold_button.set_state(filled=mode == "hold", accent=PURPLE)
        self._render_state(self.engine.enabled)
        self._log("MODE", f"Response mode set to {mode.upper()}")

    def _on_cadence_change(self, value: str) -> None:
        cadence = int(float(value))
        self.cadence_value.configure(text=f"{cadence}ms")
        self.engine.configure(cadence_ms=cadence)

    def _on_intensity_change(self, value: str) -> None:
        intensity = int(float(value))
        self.intensity_value.configure(text=f"{intensity}x")
        self.engine.configure(intensity=intensity)

    def _log(self, tag: str, message: str, alert: bool = False) -> None:
        if not hasattr(self, "log_text"):
            return
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  ", "time")
        self.log_text.insert("end", f"{tag:<6} ", "alert" if alert else "good")
        self.log_text.insert("end", f"{message}\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 80:
            self.log_text.delete("1.0", "20.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _update_uptime(self) -> None:
        if self._closing:
            return
        elapsed = int(time.monotonic() - self.started_at)
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.uptime_label.configure(text=f"UP {hours:02}:{minutes:02}:{seconds:02}")
        self.root.after(1000, self._update_uptime)

    def _start_drag(self, event: tk.Event[Any]) -> None:
        self._drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_window(self, event: tk.Event[Any]) -> None:
        x = event.x_root - self._drag_origin[0]
        y = event.y_root - self._drag_origin[1]
        self.root.geometry(f"+{x}+{y}")

    def _minimize(self) -> None:
        self.root.overrideredirect(False)
        self.root.iconify()
        self.root.bind("<Map>", self._restore_borderless, add="+")

    def _restore_borderless(self, _event: tk.Event[Any]) -> None:
        self.root.after(10, lambda: self.root.overrideredirect(True))

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.engine.stop()
        self.root.after(30, self.root.destroy)


def main() -> None:
    root = tk.Tk()
    MacroApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
