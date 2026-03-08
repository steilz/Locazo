# Locazo

A lightweight, local-only [Gyazo](https://gyazo.com) alternative for Windows. Same workflow, same hotkeys — but everything stays on your machine. No cloud, no account, no uploads.

> **Disclaimer:** This project was vibecoded with [Claude Code](https://claude.ai/claude-code). Every line of code was generated through AI-assisted development.

## Why?

Gyazo uploads every screenshot to their cloud. You have to download your own screenshots before you can use them locally. Locazo skips all of that — screenshots are saved directly to a local folder and copied to your clipboard instantly.

## Features

| Feature | Details |
|---|---|
| **Region capture** | `Ctrl+Shift+C` — crosshair cursor, drag to select, live pixel dimensions |
| **Fullscreen capture** | `Ctrl+Shift+F11` — captures primary monitor instantly |
| **Local storage** | Saves to `~/Pictures/Locazo/` — PNG by default, auto-converts to JPG if >1 MB |
| **Clipboard** | Screenshot is copied to clipboard automatically — just `Ctrl+V` anywhere |
| **Explorer integration** | Opens folder with the new file selected after each capture |
| **System tray** | Runs silently in the background with a tray icon |
| **Autostart** | Toggle Windows autostart from the tray menu |
| **Single instance** | Prevents multiple instances via Windows Mutex |
| **Multi-monitor** | Captures across all connected displays |
| **DPI aware** | Handles high-DPI / scaled displays correctly |
| **Anti-cheat safe** | No low-level keyboard hooks — uses `RegisterHotKey` and `GetAsyncKeyState` only |

## How it works

1. Press `Ctrl+Shift+C`
2. Screen freezes with a dark overlay
3. Drag to select the area you want
4. Release — screenshot is saved, copied to clipboard, and shown in Explorer

Press `ESC` or right-click to cancel at any time.

## Installation

### Option A: Use the prebuilt exe (recommended)

1. Download `Locazo.exe` from [Releases](../../releases)
2. Put it wherever you want
3. Double-click to run
4. Right-click tray icon → **Autostart** to launch with Windows

### Option B: Run from source

```bash
git clone https://github.com/steilz/Locazo.git
cd Locazo
pip install -r requirements.txt
pythonw locazo.py
```

### Option C: Build your own exe

```bash
pip install pyinstaller
python -m PyInstaller --onefile --noconsole --name Locazo --icon locazo.ico locazo.py
```

The exe will be in `dist/Locazo.exe`.

## Hotkeys

| Shortcut | Action |
|---|---|
| `Ctrl+Shift+C` | Region capture (select area) |
| `Ctrl+Shift+F11` | Fullscreen capture (primary monitor) |
| `ESC` | Cancel capture |
| Right-click | Cancel capture |

## Tray menu

| Option | Description |
|---|---|
| **Region capture** | Same as `Ctrl+Shift+C` |
| **Fullscreen** | Same as `Ctrl+Shift+F11` |
| **Open folder** | Opens the `~/Pictures/Locazo/` directory |
| **Autostart** | Toggle launch-on-boot (Windows Registry) |
| **Quit** | Exit Locazo |

Left-clicking the tray icon triggers a region capture.

## Technical details

- **Hotkeys** are registered via the Win32 `RegisterHotKey` API — only the exact key combination is captured, individual keys (Ctrl, Shift, C) pass through normally to all applications
- **ESC detection** during capture uses `GetAsyncKeyState` polling — no hooks, no message interception
- **Screen capture** uses the `mss` library for fast, multi-monitor aware grabbing
- **Overlay** is a fullscreen `tkinter` window with the frozen screenshot as background, four dark stipple rectangles create the dimming effect around the selection
- **Clipboard** uses raw Win32 API via `ctypes` (`OpenClipboard`, `SetClipboardData` with `CF_DIB`) — no `pywin32` dependency
- **Single instance** is enforced via a named Windows Mutex (`CreateMutexW`)
- **DPI awareness** is set via `SetProcessDpiAwareness(2)` (per-monitor DPI aware)

### Why not low-level keyboard hooks?

Tools like Gyazo's original C++ implementation and many Python keyboard libraries use `SetWindowsHookEx(WH_KEYBOARD_LL)` for global hotkeys. While functional, this approach:

- Gets flagged by anti-cheat software (Vanguard, EAC, BattlEye)
- Intercepts **all** keyboard input system-wide
- Can interfere with games and other applications

Locazo uses `RegisterHotKey` instead, which is the official Windows API for application hotkeys. It only captures the specific registered key combination and doesn't touch any other input.

## Dependencies

| Package | Purpose |
|---|---|
| `mss` | Fast multi-monitor screen capture |
| `Pillow` | Image processing and format conversion |
| `pystray` | System tray icon and menu |

All other functionality uses Python's standard library and Win32 API via `ctypes`.

## Requirements

- Windows 10/11
- Python 3.10+ (if running from source)

## License

MIT
