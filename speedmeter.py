"""
NetSpeed Meter - Real-time Internet Speed Monitor
===================================================
A lightweight Windows system-tray application that shows live
download/upload speed, with a detailed popup window containing
a live graph, session totals, and peak/average stats.

"""

import sys
import time
import threading
import socket
from collections import deque
from datetime import datetime

import psutil

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:
    print("ERROR: tkinter is not available. On Windows it ships with Python by default.")
    sys.exit(1)

try:
    # PIL is imported BEFORE pystray deliberately. On some Linux desktop
    # environments, pystray's tray backend (GTK/AppIndicator) initializes
    # its own font/graphics stack as a side effect of import, which can
    # interfere with PIL's text layout if a PIL font object is created
    # afterward. Importing and warming up PIL's font handling first avoids
    # any chance of that ordering issue. This has no effect on Windows,
    # where pystray uses the native Win32 tray API and there's no overlap.
    from PIL import Image, ImageDraw, ImageFont
    import pystray
except ImportError:
    print("ERROR: Missing dependencies. Run: pip install pystray pillow psutil")
    sys.exit(1)


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
POLL_INTERVAL = 1.0          # seconds between samples
GRAPH_HISTORY_LEN = 60       # how many samples to keep for the graph (60s window)
ICON_SIZE = 128              # tray icon source image size (px) - rendered large then
                              # downscaled by Windows to the actual tray size, so a bigger
                              # source with a bigger font stays crisp and readable instead
                              # of looking like tiny blurry text
LOCK_PORT = 51731            # arbitrary local port used as a single-instance mutex


def format_speed(bytes_per_sec: float) -> str:
    """Convert bytes/sec into a human readable string (KB/s or MB/s)."""
    bits = bytes_per_sec
    if bits >= 1024 * 1024:
        return f"{bits / (1024 * 1024):.2f} MB/s"
    elif bits >= 1024:
        return f"{bits / 1024:.1f} KB/s"
    else:
        return f"{bits:.0f} B/s"


def format_bytes_total(total_bytes: float) -> str:
    """Convert total bytes into human readable GB/MB."""
    if total_bytes >= 1024 ** 3:
        return f"{total_bytes / (1024 ** 3):.2f} GB"
    elif total_bytes >= 1024 ** 2:
        return f"{total_bytes / (1024 ** 2):.2f} MB"
    elif total_bytes >= 1024:
        return f"{total_bytes / 1024:.1f} KB"
    return f"{total_bytes:.0f} B"


# ----------------------------------------------------------------------------
# Single instance lock
# ----------------------------------------------------------------------------
class SingleInstanceLock:
    """
    Prevents multiple copies of the app from running at once.

    Implementation: try to bind a TCP socket on a fixed localhost port.
    Only one process can hold that binding at a time, so a second launch
    will fail to bind and knows another instance is already running.
    The socket is kept open (not closed) for the lifetime of the process;
    the OS releases it automatically when the process exits, so there's
    no stale-lock-file problem even if the app crashes.
    """

    def __init__(self, port: int = LOCK_PORT):
        self.port = port
        self._sock = None

    def acquire(self) -> bool:
        """Returns True if this is the only instance, False if another is already running."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            s.bind(("127.0.0.1", self.port))
            s.listen(1)
        except OSError:
            s.close()
            return False
        self._sock = s  # keep alive for the whole process lifetime
        return True

    def release(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


# ----------------------------------------------------------------------------
# Network Monitor (background thread)
# ----------------------------------------------------------------------------
class NetworkMonitor:
    """
    Polls psutil for network IO counters on a fixed interval and computes
    real-time upload/download speed. Runs in its own thread and stores the
    latest snapshot behind a lock.

    IMPORTANT: multiple UI components (the floating overlay, the tray icon,
    and the full stats window) all need to read this data independently and
    simultaneously. A single shared Queue does NOT work for this - whichever
    consumer happens to call get_nowait() first on a given tick drains the
    item, and the others see queue.Empty and keep showing stale data (this
    was the cause of the overlay appearing to "freeze"). Instead, we keep
    exactly one mutable "latest snapshot" dict guarded by a lock, and every
    consumer just reads a fresh copy of it on its own timer - no draining,
    no race, every consumer always sees the most recent value.
    """

    def __init__(self, interval: float = POLL_INTERVAL):
        self.interval = interval
        self._running = False
        self._thread = None

        self._lock = threading.Lock()
        self._latest_snapshot = None

        # History buffers for the graph (download speed, upload speed)
        self.download_history = deque([0] * GRAPH_HISTORY_LEN, maxlen=GRAPH_HISTORY_LEN)
        self.upload_history = deque([0] * GRAPH_HISTORY_LEN, maxlen=GRAPH_HISTORY_LEN)

        # Session stats
        self.session_start_time = time.time()
        self.session_bytes_down = 0
        self.session_bytes_up = 0
        self.peak_down = 0.0
        self.peak_up = 0.0
        self._down_samples = []
        self._up_samples = []

        self.current_down = 0.0
        self.current_up = 0.0

        self._last_counters = psutil.net_io_counters()

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def get_latest(self):
        """Thread-safe read of the most recent snapshot. Returns None before the first sample."""
        with self._lock:
            return self._latest_snapshot

    def _run(self):
        while self._running:
            time.sleep(self.interval)
            try:
                counters = psutil.net_io_counters()
            except Exception:
                continue

            bytes_down = counters.bytes_recv - self._last_counters.bytes_recv
            bytes_up = counters.bytes_sent - self._last_counters.bytes_sent
            self._last_counters = counters

            # Guard against counter resets (e.g. interface reset) producing negative deltas
            if bytes_down < 0:
                bytes_down = 0
            if bytes_up < 0:
                bytes_up = 0

            speed_down = bytes_down / self.interval
            speed_up = bytes_up / self.interval

            self.current_down = speed_down
            self.current_up = speed_up

            self.download_history.append(speed_down)
            self.upload_history.append(speed_up)

            self.session_bytes_down += bytes_down
            self.session_bytes_up += bytes_up

            self.peak_down = max(self.peak_down, speed_down)
            self.peak_up = max(self.peak_up, speed_up)

            self._down_samples.append(speed_down)
            self._up_samples.append(speed_up)

            avg_down = sum(self._down_samples) / len(self._down_samples)
            avg_up = sum(self._up_samples) / len(self._up_samples)

            snapshot = {
                "down": speed_down,
                "up": speed_up,
                "peak_down": self.peak_down,
                "peak_up": self.peak_up,
                "avg_down": avg_down,
                "avg_up": avg_up,
                "total_down": self.session_bytes_down,
                "total_up": self.session_bytes_up,
                "download_history": list(self.download_history),
                "upload_history": list(self.upload_history),
                "session_start": self.session_start_time,
            }

            with self._lock:
                self._latest_snapshot = snapshot


# ----------------------------------------------------------------------------
# Tray Icon
# ----------------------------------------------------------------------------
class TrayIconManager:
    """
    Manages the system tray icon. Generates a small bitmap on the fly
    showing the current download speed (abbreviated) so the user can
    see their speed without opening any window.
    """

    def __init__(self, on_open_callback, on_quit_callback, on_toggle_overlay_callback=None):
        self.on_open_callback = on_open_callback
        self.on_quit_callback = on_quit_callback
        self.on_toggle_overlay_callback = on_toggle_overlay_callback or (lambda: None)
        self.icon = None
        self._font_path = None
        self._font = self._load_font()

    def _load_font(self):
        # Try bold fonts first for maximum legibility at tiny tray sizes;
        # fall back to PIL default bitmap font if none are found.
        # Sized for the 128px source canvas (ICON_SIZE) - this gets downscaled
        # by Windows to the actual tray icon size, so we render text large here.
        candidates = [
            "arialbd.ttf", "Arial Bold.ttf",
            "C:\\Windows\\Fonts\\arialbd.ttf",
            "C:\\Windows\\Fonts\\seguisb.ttf",     # Segoe UI Semibold
            "C:\\Windows\\Fonts\\segoeuib.ttf",     # Segoe UI Bold
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "arial.ttf", "Arial.ttf",
        ]
        for c in candidates:
            try:
                font = ImageFont.truetype(c, 52)
                self._font_path = c
                return font
            except Exception:
                continue
        return ImageFont.load_default()

    def _font_at_size(self, size: int):
        """Re-load the resolved font file at a different point size (for shrink-to-fit)."""
        if self._font_path:
            try:
                return ImageFont.truetype(self._font_path, size)
            except Exception:
                pass
        return self._font

    def _make_icon_image(self, down_speed: float) -> "Image.Image":
        """Render a small icon with the current download speed as compact text."""
        img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Background circle - color shifts with intensity for a quick visual cue
        if down_speed >= 5 * 1024 * 1024:
            bg = (46, 160, 67, 255)      # green - fast
        elif down_speed >= 512 * 1024:
            bg = (33, 110, 200, 255)     # blue - moderate
        elif down_speed > 0:
            bg = (200, 150, 30, 255)     # amber - slow
        else:
            bg = (120, 120, 120, 255)    # grey - idle

        draw.ellipse([2, 2, ICON_SIZE - 2, ICON_SIZE - 2], fill=bg)

        # Compact label, e.g. "1M" or "340K" or "0" - kept short so bold
        # text at large font sizes still fits cleanly inside the circle
        if down_speed >= 1024 * 1024:
            mb = down_speed / (1024 * 1024)
            label = f"{mb:.0f}M" if mb >= 9.95 else f"{mb:.1f}M"
        elif down_speed >= 1024:
            label = f"{int(down_speed / 1024)}K"
        else:
            label = "0"

        # Auto-shrink font if the label would overflow the icon circle
        # (e.g. "9.9M" is wider than "0"). Start at the configured size and
        # step down until it fits, so text is always as large as possible
        # without clipping.
        max_text_width = ICON_SIZE * 0.74
        font = self._font
        font_size = getattr(font, "size", 52)

        def safe_bbox(lbl, fnt):
            """Measure text, with a sanity check against corrupted/garbage
            measurements (defensive - guards against any environment-specific
            text-shaping glitches). Falls back to a conservative estimate
            based on character count if the measurement looks invalid."""
            try:
                b = draw.textbbox((0, 0), lbl, font=fnt)
                width = b[2] - b[0]
                height = b[3] - b[1]
                if 0 <= width <= ICON_SIZE * 2 and 0 <= height <= ICON_SIZE * 2 and b[0] >= -ICON_SIZE:
                    return b
            except Exception:
                pass
            # Fallback: rough estimate, monospace-ish assumption
            fsize = getattr(fnt, "size", 30)
            est_w = int(len(lbl) * fsize * 0.62)
            est_h = int(fsize * 0.75)
            return (0, int(fsize * 0.2), est_w, int(fsize * 0.2) + est_h)

        try:
            bbox = safe_bbox(label, font)
            text_w = bbox[2] - bbox[0]
            while text_w > max_text_width and font_size > 20:
                font_size -= 4
                font = self._font_at_size(font_size)
                bbox = safe_bbox(label, font)
                text_w = bbox[2] - bbox[0]
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = (ICON_SIZE - w) / 2 - bbox[0]
            y = (ICON_SIZE - h) / 2 - bbox[1]
        except Exception:
            w, h = draw.textsize(label, font=font)
            x = (ICON_SIZE - w) / 2
            y = (ICON_SIZE - h) / 2

        draw.text((x, y), label, fill=(255, 255, 255, 255), font=font)
        return img

    def update_icon(self, down_speed: float, up_speed: float):
        if self.icon is None:
            return
        self.icon.icon = self._make_icon_image(down_speed)
        self.icon.title = (
            f"NetSpeed Meter\n"
            f"↓ {format_speed(down_speed)}   ↑ {format_speed(up_speed)}"
        )

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("Open Full Stats", lambda icon, item: self.on_open_callback()),
            pystray.MenuItem("Toggle Floating Widget", lambda icon, item: self.on_toggle_overlay_callback()),
            pystray.MenuItem("Quit", lambda icon, item: self.on_quit_callback()),
        )

    def run(self):
        """Blocking call - run the tray icon loop (call from a background thread)."""
        self.icon = pystray.Icon(
            "netspeedmeter",
            icon=self._make_icon_image(0),
            title="NetSpeed Meter",
            menu=self._build_menu(),
        )
        self.icon.run()

    def stop(self):
        if self.icon:
            self.icon.stop()


# ----------------------------------------------------------------------------
# Floating Overlay Widget (always-on-top, draggable)
# ----------------------------------------------------------------------------
class FloatingOverlay:
    """
    A small, borderless, always-on-top window that floats over everything
    else on the desktop. Shows live download/upload speed in large, readable
    text. Can be dragged anywhere by holding the left mouse button.

    Right-click brings up a tiny menu to open the full stats window or quit.
    """

    BG = "#1b1b26"
    DOWN_COLOR = "#5cb3ff"
    UP_COLOR = "#ff7a7a"
    FG = "#f5f5f5"
    BORDER = "#3a3a52"

    def __init__(self, root: tk.Tk, monitor: "NetworkMonitor",
                 on_open_full: "callable" = None, on_quit: "callable" = None):
        self.root = root
        self.monitor = monitor
        self.on_open_full = on_open_full
        self.on_quit = on_quit

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)        # no title bar / borders
        self.win.attributes("-topmost", True)   # always on top
        try:
            self.win.attributes("-alpha", 0.92)  # slight transparency (Windows supports this)
        except tk.TclError:
            pass
        self.win.configure(bg=self.BORDER)

        # Default position: top-right corner of the screen
        screen_w = self.win.winfo_screenwidth()
        self._width, self._height = 190, 64
        x = screen_w - self._width - 20
        y = 20
        self.win.geometry(f"{self._width}x{self._height}+{x}+{y}")

        self._build_ui()
        self._bind_drag()
        self._bind_menu()

        self._poll_updates()

    # ---- UI -----------------------------------------------------------
    def _build_ui(self):
        # 1px border effect via outer frame (BORDER color) containing inner padded frame
        inner = tk.Frame(self.win, bg=self.BG)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        row = tk.Frame(inner, bg=self.BG)
        row.pack(fill="both", expand=True, padx=10, pady=6)

        # Download column
        down_col = tk.Frame(row, bg=self.BG)
        down_col.pack(side="left", fill="both", expand=True)
        tk.Label(down_col, text="\u2193 DOWN", font=("Segoe UI", 8, "bold"),
                 bg=self.BG, fg=self.DOWN_COLOR).pack(anchor="w")
        self.down_label = tk.Label(down_col, text="0 B/s", font=("Consolas", 14, "bold"),
                                    bg=self.BG, fg=self.FG)
        self.down_label.pack(anchor="w")

        # Upload column
        up_col = tk.Frame(row, bg=self.BG)
        up_col.pack(side="left", fill="both", expand=True)
        tk.Label(up_col, text="\u2191 UP", font=("Segoe UI", 8, "bold"),
                 bg=self.BG, fg=self.UP_COLOR).pack(anchor="w")
        self.up_label = tk.Label(up_col, text="0 B/s", font=("Consolas", 14, "bold"),
                                  bg=self.BG, fg=self.FG)
        self.up_label.pack(anchor="w")

        # Small drag-hint strip at top (also draggable, makes it obvious this thing moves)
        self.grip = tk.Frame(inner, bg=self.BORDER, height=4, cursor="fleur")
        self.grip.pack(fill="x", side="top")

        # Make every widget draggable, not just the grip
        self._draggable_widgets = [self.win, inner, row, down_col, up_col, self.grip]

    # ---- Dragging -------------------------------------------------------
    def _bind_drag(self):
        self._drag_offset = (0, 0)

        def start_drag(event):
            self._drag_offset = (event.x_root - self.win.winfo_x(),
                                  event.y_root - self.win.winfo_y())

        def do_drag(event):
            x = event.x_root - self._drag_offset[0]
            y = event.y_root - self._drag_offset[1]
            self.win.geometry(f"+{x}+{y}")

        for widget in self._draggable_widgets:
            widget.bind("<ButtonPress-1>", start_drag)
            widget.bind("<B1-Motion>", do_drag)

    # ---- Right-click menu -------------------------------------------------
    def _bind_menu(self):
        menu = tk.Menu(self.win, tearoff=0)
        menu.add_command(label="Open Full Stats", command=lambda: self.on_open_full and self.on_open_full())
        menu.add_separator()
        menu.add_command(label="Quit NetSpeed Meter", command=lambda: self.on_quit and self.on_quit())

        def show_menu(event):
            menu.tk_popup(event.x_root, event.y_root)

        for widget in self._draggable_widgets:
            widget.bind("<ButtonPress-3>", show_menu)

    # ---- Live updates -------------------------------------------------------
    def _poll_updates(self):
        latest = self.monitor.get_latest()

        if latest:
            self.down_label.config(text=format_speed(latest["down"]))
            self.up_label.config(text=format_speed(latest["up"]))

        # Re-assert topmost + lift every tick. overrideredirect windows on
        # Windows can silently lose their "always on top" status when the
        # taskbar, another always-on-top window, or a fullscreen app
        # re-asserts its own z-order. Setting -topmost once at creation time
        # is not reliable; toggling it off/on and lifting forces Windows to
        # re-evaluate the stacking order every second, which keeps the
        # overlay reliably above the taskbar.
        try:
            self.win.attributes("-topmost", False)
            self.win.attributes("-topmost", True)
            self.win.lift()
        except tk.TclError:
            pass

        self.root.after(int(POLL_INTERVAL * 1000), self._poll_updates)

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Popup Window (Tkinter)
# ----------------------------------------------------------------------------
class SpeedMeterWindow:
    """
    The detailed stats window shown when the user clicks the tray icon.
    Contains big current-speed readouts, a live dual-line graph (canvas-based,
    no extra plotting library required), and session totals.
    """

    BG = "#1e1e2e"
    FG = "#f5f5f5"
    DOWN_COLOR = "#4fa3ff"
    UP_COLOR = "#ff6b6b"
    GRID_COLOR = "#33334d"
    PANEL_BG = "#262638"

    def __init__(self, root: tk.Tk, monitor: NetworkMonitor):
        self.root = root
        self.monitor = monitor
        self.visible = False

        self.root.title("NetSpeed Meter")
        self.root.geometry("520x600")
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.resizable(False, False)

        self._build_ui()
        self.hide()  # start hidden, tray click reveals it
        self._poll_updates()

    # ---- UI construction -------------------------------------------------
    def _build_ui(self):
        header = tk.Label(
            self.root, text="🌐 NetSpeed Meter", font=("Segoe UI", 16, "bold"),
            bg=self.BG, fg=self.FG, pady=10
        )
        header.pack(fill="x")

        # Current speed readouts
        readout_frame = tk.Frame(self.root, bg=self.BG)
        readout_frame.pack(fill="x", padx=20, pady=(0, 10))

        self.down_label = self._make_readout(readout_frame, "↓ DOWNLOAD", self.DOWN_COLOR, side="left")
        self.up_label = self._make_readout(readout_frame, "↑ UPLOAD", self.UP_COLOR, side="right")

        # Graph canvas
        self.canvas = tk.Canvas(self.root, width=480, height=180, bg=self.PANEL_BG, highlightthickness=0)
        self.canvas.pack(padx=20, pady=10)

        # Stats grid (peak / avg / total / session time)
        stats_frame = tk.Frame(self.root, bg=self.BG)
        stats_frame.pack(fill="x", padx=20, pady=(5, 10))

        self.stat_vars = {}
        labels = [
            ("Peak ↓", "peak_down"), ("Peak ↑", "peak_up"),
            ("Avg ↓", "avg_down"), ("Avg ↑", "avg_up"),
            ("Total ↓", "total_down"), ("Total ↑", "total_up"),
        ]
        for i, (label_text, key) in enumerate(labels):
            row, col = divmod(i, 2)
            cell = tk.Frame(stats_frame, bg=self.PANEL_BG, padx=10, pady=6)
            cell.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
            stats_frame.grid_columnconfigure(col, weight=1)

            tk.Label(cell, text=label_text, font=("Segoe UI", 9), bg=self.PANEL_BG, fg="#aaaaaa").pack(anchor="w")
            val_label = tk.Label(cell, text="--", font=("Segoe UI", 12, "bold"), bg=self.PANEL_BG, fg=self.FG)
            val_label.pack(anchor="w")
            self.stat_vars[key] = val_label

        self.session_label = tk.Label(
            self.root, text="Session started: --", font=("Segoe UI", 9),
            bg=self.BG, fg="#888888"
        )
        self.session_label.pack(pady=(0, 10))

    def _make_readout(self, parent, title, color, side):
        frame = tk.Frame(parent, bg=self.BG)
        frame.pack(side=side, expand=True)
        tk.Label(frame, text=title, font=("Segoe UI", 10, "bold"), bg=self.BG, fg=color).pack()
        value_label = tk.Label(frame, text="0 B/s", font=("Segoe UI", 22, "bold"), bg=self.BG, fg=self.FG)
        value_label.pack()
        return value_label

    # ---- Graph drawing -----------------------------------------------------
    def _draw_graph(self, down_history, up_history):
        self.canvas.delete("all")
        w, h = 480, 180
        padding = 10

        max_val = max(max(down_history, default=0), max(up_history, default=0), 1)

        # grid lines
        for frac in (0.25, 0.5, 0.75):
            y = h - padding - frac * (h - 2 * padding)
            self.canvas.create_line(padding, y, w - padding, y, fill=self.GRID_COLOR, dash=(2, 2))

        def to_points(history):
            n = len(history)
            if n < 2:
                return []
            step = (w - 2 * padding) / (n - 1)
            pts = []
            for i, val in enumerate(history):
                x = padding + i * step
                y = h - padding - (val / max_val) * (h - 2 * padding)
                pts.append((x, y))
            return pts

        for history, color in ((down_history, self.DOWN_COLOR), (up_history, self.UP_COLOR)):
            pts = to_points(history)
            if len(pts) >= 2:
                flat = [coord for point in pts for coord in point]
                self.canvas.create_line(*flat, fill=color, width=2, smooth=True)

        # max value label
        self.canvas.create_text(
            w - padding - 4, padding + 8,
            text=format_speed(max_val), fill="#888888", anchor="ne", font=("Segoe UI", 8)
        )

    # ---- Update loop --------------------------------------------------------
    def _poll_updates(self):
        """Reads the monitor's latest snapshot and refreshes the UI. Runs on the Tk main loop."""
        latest = self.monitor.get_latest()

        if latest:
            self.down_label.config(text=format_speed(latest["down"]))
            self.up_label.config(text=format_speed(latest["up"]))

            self.stat_vars["peak_down"].config(text=format_speed(latest["peak_down"]))
            self.stat_vars["peak_up"].config(text=format_speed(latest["peak_up"]))
            self.stat_vars["avg_down"].config(text=format_speed(latest["avg_down"]))
            self.stat_vars["avg_up"].config(text=format_speed(latest["avg_up"]))
            self.stat_vars["total_down"].config(text=format_bytes_total(latest["total_down"]))
            self.stat_vars["total_up"].config(text=format_bytes_total(latest["total_up"]))

            start_str = datetime.fromtimestamp(latest["session_start"]).strftime("%H:%M:%S")
            self.session_label.config(text=f"Session started: {start_str}")

            self._draw_graph(latest["download_history"], latest["upload_history"])

            # Update tray icon too
            if self.on_tray_update:
                self.on_tray_update(latest["down"], latest["up"])

        # Reschedule
        self.root.after(int(POLL_INTERVAL * 1000), self._poll_updates)

    # ---- Visibility ----------------------------------------------------------
    on_tray_update = None  # set externally by main()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.visible = True

    def hide(self):
        self.root.withdraw()
        self.visible = False

    def toggle(self):
        if self.visible:
            self.hide()
        else:
            self.show()


# ----------------------------------------------------------------------------
# Application entry point
# ----------------------------------------------------------------------------
def main():
    # --- Single instance guard -------------------------------------------
    lock = SingleInstanceLock()
    if not lock.acquire():
        # Another copy is already running. Show a brief, self-dismissing
        # notice (auto-closes - never blocks waiting for a click) then exit.
        try:
            _root = tk.Tk()
            _root.overrideredirect(True)
            _root.attributes("-topmost", True)
            w, h = 320, 80
            x = (_root.winfo_screenwidth() - w) // 2
            y = 60
            _root.geometry(f"{w}x{h}+{x}+{y}")
            _root.configure(bg="#1b1b26")
            tk.Label(
                _root, text="NetSpeed Meter is already running.\nCheck your system tray / floating widget.",
                bg="#1b1b26", fg="#f5f5f5", font=("Segoe UI", 10), justify="center"
            ).pack(expand=True, fill="both", padx=12, pady=12)
            _root.after(2500, _root.destroy)
            _root.mainloop()
        except Exception:
            print("NetSpeed Meter is already running. Check your system tray.")
        sys.exit(0)

    monitor = NetworkMonitor()
    monitor.start()

    root = tk.Tk()
    root.withdraw()  # root window itself is never shown; it just drives the Tk event loop

    window = SpeedMeterWindow(root, monitor)

    overlay_holder = {"overlay": None}

    def show_overlay():
        if overlay_holder["overlay"] is None:
            overlay_holder["overlay"] = FloatingOverlay(
                root, monitor,
                on_open_full=lambda: root.after(0, window.show),
                on_quit=lambda: root.after(0, lambda: shutdown()),
            )

    def hide_overlay():
        if overlay_holder["overlay"] is not None:
            overlay_holder["overlay"].destroy()
            overlay_holder["overlay"] = None

    def toggle_overlay():
        if overlay_holder["overlay"] is None:
            show_overlay()
        else:
            hide_overlay()

    tray = TrayIconManager(
        on_open_callback=lambda: root.after(0, window.show),
        on_quit_callback=lambda: root.after(0, shutdown),
        on_toggle_overlay_callback=lambda: root.after(0, toggle_overlay),
    )
    window.on_tray_update = tray.update_icon

    # Tray icon must run its own loop; do it in a daemon thread so Tk owns the main thread.
    tray_thread = threading.Thread(target=tray.run, daemon=True)
    tray_thread.start()

    # Floating overlay is shown by default on launch - that's the primary UI now.
    show_overlay()

    def shutdown():
        monitor.stop()
        tray.stop()
        lock.release()
        root.quit()
        root.destroy()
        sys.exit(0)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
