# NetSpeed Meter

A real-time internet speed monitor for Windows. A small floating widget sits on top of your desktop showing live download/upload speed in big, readable numbers — drag it anywhere you like. A system tray icon runs alongside it for quick access to the full stats window.

## Features

- **Floating widget** — always-on-top, draggable, shows live ↓ download / ↑ upload speed in large readable text. Drag it by clicking and holding anywhere on it. Right-click for a quick menu.
- **Single instance only** — running the app again while it's already open won't create a second copy or duplicate tray icon. You'll just see a brief "already running" notice that disappears on its own.
- **System tray icon** — right-click (or click, depending on Windows version) for:
  - **Open Full Stats** — detailed window with a live 60-second graph, peak/average speeds, and total data used this session
  - **Toggle Floating Widget** — show or hide the floating widget
  - **Quit** — closes the app completely
- **Real data** — reads directly from Windows network interface counters via `psutil`, the same data source Task Manager uses. No simulated numbers.
- **Lightweight** — pure Python, polls once per second, minimal CPU/memory footprint

## Requirements

- Windows 10 or 11
- Python 3.8 or newer ([download here](https://www.python.org/downloads/) if you don't have it — check "Add Python to PATH" during install)

## Quick Start (Option A: Run with Python)

1. Extract all files into a folder, e.g. `C:\NetSpeedMeter\`
2. Double-click **`run.bat`**
   - First run will take ~30 seconds to set up a virtual environment and install dependencies (`psutil`, `pystray`, `pillow`)
   - After that, it launches instantly
3. The floating widget appears in the top-right corner of your screen immediately. **Drag it anywhere** — just click and hold on it, then move your mouse.
4. Look for the icon in your system tray too (bottom-right, near the clock — click the **^** arrow if hidden) for the full stats window and other options.
5. To quit, right-click the floating widget → **Quit NetSpeed Meter**, or use the tray icon menu → **Quit**.

### Running it again while it's already open

Nothing bad happens — you'll see a small notice saying it's already running, which disappears after about 2 seconds on its own. No duplicate widgets or tray icons will be created.

## Build a Standalone .exe (Option B: No Python needed afterward)

If you want a single `.exe` file you can run on any Windows PC without Python installed:

1. Make sure Python is installed on the build machine (just for building, not for running afterward)
2. Double-click **`build_exe.bat`**
3. Wait for the build to finish (~1-2 minutes)
4. Your standalone app will be at **`dist\NetSpeedMeter.exe`**
5. Copy that single file anywhere — even to a USB drive or another PC — and double-click to run

Tip: place a shortcut to the `.exe` in your Windows Startup folder (`Win+R` → `shell:startup`) if you want the widget to launch automatically every time you log in.

Alternative way:
Make an executing(.exe) file:
  1. Open the folder in the powershell
  2. Run this command py -m PyInstaller --onefile --noconsole speedmeter.py
It will create an exe file in that folder

## Files in this package

| File | Purpose |
|---|---|
| `speedmeter.py` | The full application source code |
| `requirements.txt` | Python dependencies |
| `run.bat` | One-click setup + launch (for running with Python installed) |
| `build_exe.bat` | One-click build into a standalone `.exe` via PyInstaller |
| `README.md` | This file |

## How it works (technical notes)

- Uses `psutil.net_io_counters()` to read total bytes sent/received across all network interfaces, sampled once per second. The difference between consecutive samples gives bytes/sec, which is converted to KB/s or MB/s.
- **Single-instance lock**: the app tries to bind a TCP socket on a fixed local port (127.0.0.1) when it starts. Only one process can hold that binding at a time, so a second launch detects the failure immediately and exits after a brief notice. The OS releases the port automatically if the app closes or crashes, so there's never a stale lock to clean up manually.
- The **floating widget** is a borderless, topmost `tkinter` window (no title bar, slightly transparent) that you can drag by clicking anywhere on it. It shows the same live numbers as the tray, just much larger and easier to read.
- The **tray icon** is a small in-memory image generated fresh every second with `Pillow`, showing an abbreviated speed label and color-coded by intensity, mainly as a secondary glance indicator and menu access point.
- The **full stats window** is built with `tkinter` and draws its own live line graph on a `Canvas` — no charting library dependency required.
- A background thread handles all the polling so the UI never freezes; updates are passed to the main thread through a thread-safe queue.

## Troubleshooting

**"python is not recognized"**
Python isn't on your PATH. Reinstall Python and check "Add Python to PATH", or use the full path to python.exe in the .bat file.

**I can't find the floating widget**
It starts in the top-right corner of your primary monitor. If you have multiple monitors or changed resolution since last placing it, it might be off-screen — right-click the tray icon → **Toggle Floating Widget** twice to reset it back to the default position.

**Tray icon doesn't show up**
Click the **^** (show hidden icons) arrow in the taskbar near the clock — Windows sometimes hides new tray icons by default. You can drag it out to always show.

**Numbers look different from other speed test sites**
This tool measures *your computer's actual network throughput* (all apps combined), not a single speed-test connection to one server. A speed test (like Ookla) opens a dedicated connection to a nearby server to measure your line's maximum capacity. This tool shows real, current usage — so it'll read near 0 when idle and spike up only while something is actually transferring data (downloads, streaming, updates, etc).

**Antivirus flags the .exe**
This is a common false positive with PyInstaller-built executables since the packing method resembles techniques some malware uses. The source code is fully readable in `speedmeter.py` — you can inspect it yourself, or run it via `run.bat` instead of building the `.exe` if you'd prefer not to deal with this.
