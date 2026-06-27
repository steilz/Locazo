# Locazo

A lightweight, local-only [Gyazo](https://gyazo.com) alternative. Same workflow, same hotkeys — but everything stays on your machine. No cloud, no account, no uploads.

Locazo ships two native implementations that share the same workflow and hotkeys:

| Platform | Entry point | Tech |
|---|---|---|
| **Windows 10/11** | `locazo.py` | Win32 (`ctypes`), `mss`, `tkinter` overlay, `pystray` |
| **Linux (X11)** | `locazo_linux.py` | GTK3/GDK + Cairo overlay, `xclip`, Ayatana AppIndicator, `python-xlib` |

> **Disclaimer:** This project was vibecoded with [Claude Code](https://claude.ai/claude-code). Every line of code was generated through AI-assisted development.

## Why?

Gyazo uploads every screenshot to their cloud. You have to download your own screenshots before you can use them locally. Locazo skips all of that — screenshots are saved directly to a local folder and copied to your clipboard instantly.

## Shared features

| Feature | Details |
|---|---|
| **Region capture** | `Ctrl+Shift+C` — drag to select an area |
| **Fullscreen capture** | `Ctrl+Shift+F11` — captures the screen instantly |
| **Local storage** | Saves to `~/Pictures/Locazo/` — PNG by default, auto-converts to JPG if >1 MB |
| **Clipboard** | Screenshot is copied to the clipboard automatically — just `Ctrl+V` anywhere |
| **File manager** | Opens the screenshot folder after each capture |
| **System tray** | Runs silently in the background with a tray icon |
| **Autostart** | Toggle launch-on-boot from the tray menu |
| **Single instance** | Only one Locazo runs at a time |

---

## Windows

A lightweight local Gyazo alternative for Windows. Multi-monitor and DPI aware, anti-cheat safe (no low-level keyboard hooks — uses `RegisterHotKey` only).

### How it works

1. Press `Ctrl+Shift+C`
2. Screen freezes with a dark overlay
3. Drag to select the area you want
4. Release — screenshot is saved, copied to clipboard, and shown in Explorer

Press `ESC` or right-click to cancel at any time.

### Installation

**Option A: Use the prebuilt exe (recommended)**

1. Download `Locazo.exe` from [Releases](../../releases)
2. Put it wherever you want and double-click to run
3. Right-click tray icon → **Autostart** to launch with Windows

**Option B: Run from source**

```bash
git clone https://github.com/steilz/Locazo.git
cd Locazo
pip install -r requirements.txt
pythonw locazo.py
```

**Option C: Build your own exe**

```bash
pip install pyinstaller
python -m PyInstaller --onefile --noconsole --name Locazo --icon locazo.ico locazo.py
```

The exe will be in `dist/Locazo.exe`.

### Technical details

- **Hotkeys** are registered via the Win32 `RegisterHotKey` API — only the exact key combination is captured, individual keys (Ctrl, Shift, C) pass through normally to all applications
- **ESC detection** during capture uses a dedicated `RegisterHotKey` listener thread (plus a tkinter `<Escape>` binding as fallback) — no hooks, no message interception
- **Screen capture** uses the `mss` library for fast, multi-monitor aware grabbing
- **Overlay** is a fullscreen `tkinter` window showing a pre-darkened copy of the frozen screen; the bright original shows through only inside the live selection, so dragging stays smooth (no per-frame stipple rendering)
- **Clipboard** uses raw Win32 API via `ctypes` (`OpenClipboard`, `SetClipboardData` with `CF_DIB`) — no `pywin32` dependency
- **Single instance** is enforced via a named Windows Mutex (`CreateMutexW`)
- **DPI awareness** is set via `SetProcessDpiAwareness(2)` (per-monitor DPI aware)

#### Why not low-level keyboard hooks?

Tools like Gyazo's original C++ implementation and many Python keyboard libraries use `SetWindowsHookEx(WH_KEYBOARD_LL)` for global hotkeys. While functional, this approach gets flagged by anti-cheat software (Vanguard, EAC, BattlEye), intercepts **all** keyboard input system-wide, and can interfere with games. Locazo uses `RegisterHotKey` instead — the official Windows API for application hotkeys — which only captures the specific registered combination.

### Windows dependencies

| Package | Purpose |
|---|---|
| `mss` | Fast multi-monitor screen capture |
| `Pillow` | Image processing and format conversion |
| `pystray` | System tray icon and menu |

Requirements: Windows 10/11, Python 3.10+ (if running from source).

---

## Linux (X11)

Reimplemented natively for X11 desktops (developed on Linux Mint Cinnamon). The tray lives in an Ayatana AppIndicator, global hotkeys are grabbed via `python-xlib`, and region selection uses a custom GTK/Cairo overlay that mirrors the smooth Windows selector — no `gnome-screenshot` dependency, no per-frame flicker.

### How it works

1. Press `Ctrl+Shift+C`
2. The screen freezes with a dark overlay
3. Drag to select the area you want — the bright original shows through inside the selection, with live pixel dimensions
4. Release — screenshot is saved, copied to the clipboard with `xclip`, and the folder opens

Press `ESC` or right-click to cancel at any time.

### Installation

Install the system dependencies, then the Python packages:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
                 gir1.2-ayatanaappindicator3-0.1 xclip xdg-utils
git clone https://github.com/steilz/Locazo.git
cd Locazo
pip install -r requirements-linux.txt
python3 locazo_linux.py
```

Enable **Autostart** from the tray menu to launch on login (writes `~/.config/autostart/locazo.desktop`).

### Technical details

- **Hotkeys** are grabbed globally via `python-xlib` (`grab_key` on the root window), accounting for Lock/NumLock modifier combinations
- **Screen capture** grabs the root window with GDK (`Gdk.pixbuf_get_from_window`) — multi-monitor aware, no external process
- **Overlay** is a borderless GTK window: the grabbed screen is pre-darkened into a Cairo surface once, and the bright original is blitted only inside the live selection via a clip; only the changed region is invalidated per motion event, so dragging stays smooth across monitors
- **Input** during selection is confined with a GDK seat grab (crosshair cursor); `ESC` or right-click cancels
- **Clipboard** pipes the saved image into `xclip` with the correct MIME target (`image/png` or `image/jpeg`)
- **File manager** opens via `xdg-open`
- **Single instance** is enforced with an `fcntl` file lock on `/tmp/locazo.lock`
- **Tray** uses GTK3 + Ayatana AppIndicator
- **Logging** — failures are written to `~/Pictures/Locazo/locazo.log` instead of being silently swallowed

### Linux dependencies

Python packages (`requirements-linux.txt`): `Pillow`, `python-xlib`.

System packages: `python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-3.0`, `gir1.2-ayatanaappindicator3-0.1`, `xclip`, `xdg-utils`.

Requirements: a Linux X11 session (Wayland is not supported), Python 3.10+.

---

## Hotkeys (both platforms)

| Shortcut | Action |
|---|---|
| `Ctrl+Shift+C` | Region capture (select area) |
| `Ctrl+Shift+F11` | Fullscreen capture |

## Tray menu

| Option | Description |
|---|---|
| **Region capture** | Same as `Ctrl+Shift+C` |
| **Fullscreen** | Same as `Ctrl+Shift+F11` |
| **Open folder** | Opens the `~/Pictures/Locazo/` directory |
| **Autostart** | Toggle launch-on-boot |
| **Quit** | Exit Locazo |

## License

MIT
