"""
Locazo - Local Screenshot Tool
A lightweight Gyazo alternative that saves screenshots locally.

Hotkeys:
  Ctrl+Shift+C    - Region capture (select area)
  Ctrl+Shift+F11  - Fullscreen capture (primary monitor)

System tray icon with right-click menu for all actions.
Left-click tray icon = region capture.
"""

import ctypes
import ctypes.wintypes
import io
import os
import sys
import threading
import tkinter as tk
import winreg
from datetime import datetime
from pathlib import Path

import mss
from PIL import Image, ImageDraw, ImageTk
import pystray

# ── Single Instance Check ─────────────────────────────────────────────
_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "LocazoSingleInstanceMutex")
if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    sys.exit(0)

# ── DPI Awareness (must be set before any GUI operations) ─────────────
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ── Configuration ─────────────────────────────────────────────────────
SAVE_DIR = Path.home() / "Pictures" / "Locazo"
APP_NAME = "Locazo"
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

# Win32 hotkey constants
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
VK_C = 0x43
VK_ESCAPE = 0x1B
VK_F11 = 0x7A
WM_HOTKEY = 0x0312
HOTKEY_ID_REGION = 1
HOTKEY_ID_FULLSCREEN = 2


# ── Clipboard (pure ctypes, no pywin32 needed) ───────────────────────
def copy_image_to_clipboard(image: Image.Image):
    """Copy a PIL Image to the Windows clipboard as CF_DIB."""
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]  # Skip 14-byte BMP file header
    buf.close()

    CF_DIB = 8
    GMEM_MOVEABLE = 0x0002

    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p

    if u32.OpenClipboard(0):
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if h:
            p = k32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            u32.SetClipboardData(CF_DIB, h)
        u32.CloseClipboard()


# ── Tray Icon ────────────────────────────────────────────────────────
def make_tray_icon() -> Image.Image:
    """Generate a tray icon: blue rounded square with white crosshair."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Blue background
    d.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(66, 133, 244))
    # Crosshair
    cx, cy = 32, 32
    d.line([(cx - 14, cy), (cx + 14, cy)], fill="white", width=2)
    d.line([(cx, cy - 14), (cx, cy + 14)], fill="white", width=2)
    # Center circle
    d.ellipse([(cx - 5, cy - 5), (cx + 5, cy + 5)], outline="white", width=2)
    return img


# ── Selection Overlay ────────────────────────────────────────────────
class SelectionOverlay:
    """Fullscreen overlay for interactive region selection.

    Shows a frozen screenshot with a dark tint. The user drags to select
    a bright rectangle. Dimensions are shown live. ESC / right-click cancels.
    """

    def __init__(self, callback):
        """callback receives a PIL Image on success, or None on cancel."""
        self.callback = callback

    # ── public ──

    def show(self):
        """Capture screen, display overlay, enter mainloop (blocking)."""
        # Grab all monitors as one image
        with mss.mss() as sct:
            mon = sct.monitors[0]  # virtual screen spanning all monitors
            raw = sct.grab(mon)
            self.screenshot = Image.frombytes("RGB", raw.size, raw.rgb)
            self.mon_left = mon["left"]
            self.mon_top = mon["top"]
            self.mon_w = mon["width"]
            self.mon_h = mon["height"]

        self._build_ui()
        self.root.mainloop()

    # ── UI setup ──

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry(
            f"{self.mon_w}x{self.mon_h}+{self.mon_left}+{self.mon_top}"
        )

        self.canvas = tk.Canvas(
            self.root,
            width=self.mon_w,
            height=self.mon_h,
            cursor="cross",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack()

        # Background: the frozen screenshot
        self.photo = ImageTk.PhotoImage(self.screenshot)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        # Dark overlay – 4 rectangles that form a frame around the selection
        # Initially one big rect covers everything; during drag they reshape
        stip = "gray50"
        self.d_top = self.canvas.create_rectangle(
            0, 0, self.mon_w, self.mon_h, fill="black", outline="", stipple=stip
        )
        self.d_bot = self.canvas.create_rectangle(
            0, 0, 0, 0, fill="black", outline="", stipple=stip
        )
        self.d_lft = self.canvas.create_rectangle(
            0, 0, 0, 0, fill="black", outline="", stipple=stip
        )
        self.d_rgt = self.canvas.create_rectangle(
            0, 0, 0, 0, fill="black", outline="", stipple=stip
        )

        # Selection rectangle (blue border)
        self.sel_rect = self.canvas.create_rectangle(
            0, 0, 0, 0, outline="#00aaff", width=2
        )

        # Dimension label
        self.dim_bg = self.canvas.create_rectangle(
            0, 0, 0, 0, fill="#1a1a2e", outline=""
        )
        self.dim_txt = self.canvas.create_text(
            0, 0, text="", fill="#00aaff", font=("Segoe UI", 10, "bold"), anchor="nw"
        )

        # State
        self.sx = self.sy = 0
        self.dragging = False
        self._done = False

        # Mouse bindings
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>", lambda _: self._finish(None))

        # ESC via GetAsyncKeyState polling (no hooks, no messages, anti-cheat safe)
        self._poll_esc()

        self.root.deiconify()
        self.root.focus_force()

    # ── event handlers ──

    def _on_press(self, e):
        self.sx, self.sy = e.x, e.y
        self.dragging = True

    def _on_drag(self, e):
        if not self.dragging:
            return

        x1, y1 = min(self.sx, e.x), min(self.sy, e.y)
        x2, y2 = max(self.sx, e.x), max(self.sy, e.y)

        # Update selection rectangle
        self.canvas.coords(self.sel_rect, x1, y1, x2, y2)

        # Reshape dark overlay (4 rects around selection)
        w, h = self.mon_w, self.mon_h
        self.canvas.coords(self.d_top, 0, 0, w, y1)        # above selection
        self.canvas.coords(self.d_bot, 0, y2, w, h)        # below selection
        self.canvas.coords(self.d_lft, 0, y1, x1, y2)      # left of selection
        self.canvas.coords(self.d_rgt, x2, y1, w, y2)       # right of selection

        # Dimension label
        pw, ph = x2 - x1, y2 - y1
        label = f"{pw} \u00d7 {ph}"
        self.canvas.itemconfigure(self.dim_txt, text=label)

        # Position label near bottom-right of selection
        tx, ty = x2 + 8, y2 + 8
        if tx + 100 > w:
            tx = x1 - 100
        if ty + 24 > h:
            ty = y1 - 24

        self.canvas.coords(self.dim_txt, tx, ty)
        # Use actual text bbox for the background
        bbox = self.canvas.bbox(self.dim_txt)
        if bbox:
            self.canvas.coords(
                self.dim_bg, bbox[0] - 4, bbox[1] - 2, bbox[2] + 4, bbox[3] + 2
            )

    def _on_release(self, e):
        if not self.dragging:
            return
        x1, y1 = min(self.sx, e.x), min(self.sy, e.y)
        x2, y2 = max(self.sx, e.x), max(self.sy, e.y)

        if (x2 - x1) > 5 and (y2 - y1) > 5:
            self._finish(self.screenshot.crop((x1, y1, x2, y2)))
        else:
            self._finish(None)

    def _poll_esc(self):
        """Poll ESC key state via GetAsyncKeyState – no hooks, no messages."""
        if self._done:
            return
        if ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
            self._finish(None)
            return
        self.root.after(20, self._poll_esc)

    def _finish(self, result):
        """Clean up overlay and deliver result. Safe to call multiple times."""
        if self._done:
            return
        self._done = True
        self.root.destroy()
        self.callback(result)


# ── Main Application ─────────────────────────────────────────────────
class Locazo:
    """System-tray screenshot application."""

    def __init__(self):
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        self.capturing = False
        self.icon = None

    # ── lifecycle ──

    def run(self):
        """Start tray icon and hotkey listeners (blocking)."""
        menu = pystray.Menu(
            pystray.MenuItem(
                "Region capture\tCtrl+Shift+C", self._region, default=True
            ),
            pystray.MenuItem("Fullscreen\tCtrl+Shift+F11", self._fullscreen),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open folder", self._open_folder),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Autostart",
                self._toggle_autostart,
                checked=lambda _: self._autostart_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self.icon = pystray.Icon(APP_NAME, make_tray_icon(), APP_NAME, menu)
        self.icon.run(setup=self._setup)

    def _setup(self, icon):
        icon.visible = True
        threading.Thread(target=self._hotkey_loop, daemon=True).start()

    def _hotkey_loop(self):
        """Listen for global hotkeys via Win32 RegisterHotKey.

        Only consumes the exact key combo – individual Ctrl/Shift pass through.
        """
        u32 = ctypes.windll.user32
        mods = MOD_CONTROL | MOD_SHIFT
        u32.RegisterHotKey(None, HOTKEY_ID_REGION, mods, VK_C)
        u32.RegisterHotKey(None, HOTKEY_ID_FULLSCREEN, mods, VK_F11)
        self._hotkey_tid = ctypes.windll.kernel32.GetCurrentThreadId()

        msg = ctypes.wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                if msg.wParam == HOTKEY_ID_REGION:
                    self._region()
                elif msg.wParam == HOTKEY_ID_FULLSCREEN:
                    self._fullscreen()

        u32.UnregisterHotKey(None, HOTKEY_ID_REGION)
        u32.UnregisterHotKey(None, HOTKEY_ID_FULLSCREEN)

    # ── capture actions ──

    def _region(self, *_):
        if self.capturing:
            return
        self.capturing = True
        threading.Thread(target=self._region_thread, daemon=True).start()

    def _region_thread(self):
        def on_result(img):
            if img:
                self._save(img)
            self.capturing = False

        SelectionOverlay(on_result).show()

    def _fullscreen(self, *_):
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])  # primary monitor
            self._save(Image.frombytes("RGB", raw.size, raw.rgb))

    # ── save & open ──

    def _save(self, img: Image.Image):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = SAVE_DIR / f"Locazo_{ts}.png"

        img.save(str(path), "PNG")

        # Auto-convert to JPG if over 1 MB (like Gyazo)
        if path.stat().st_size > 1_000_000:
            jpg = path.with_suffix(".jpg")
            img.save(str(jpg), "JPEG", quality=90)
            path.unlink()
            path = jpg

        # Copy image to clipboard
        try:
            copy_image_to_clipboard(img)
        except Exception:
            pass

        # Open folder and select the new file
        try:
            self._show_in_explorer(path)
        except Exception:
            pass

    # ── explorer ──

    @staticmethod
    def _show_in_explorer(path: Path):
        """Open folder & select file via Shell API. Reuses existing windows."""
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32

        ole32.CoInitialize(None)
        try:
            pidl_folder = ctypes.c_void_p()
            pidl_file = ctypes.c_void_p()

            shell32.SHParseDisplayName(
                str(path.parent), None, ctypes.byref(pidl_folder), 0, None
            )
            shell32.SHParseDisplayName(
                str(path), None, ctypes.byref(pidl_file), 0, None
            )

            if pidl_folder and pidl_file:
                pidl_array = (ctypes.c_void_p * 1)(pidl_file)
                shell32.SHOpenFolderAndSelectItems(
                    pidl_folder, 1, pidl_array, 0
                )

            for pidl in (pidl_folder, pidl_file):
                if pidl:
                    ole32.CoTaskMemFree(pidl)
        finally:
            ole32.CoUninitialize()

    def _open_folder(self, *_):
        os.startfile(str(SAVE_DIR))

    # ── autostart (Windows registry) ──

    def _toggle_autostart(self, *_):
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_ALL_ACCESS
            )
            if self._autostart_enabled():
                winreg.DeleteValue(key, APP_NAME)
            else:
                # Always point to Locazo.exe next to this script/exe
                if getattr(sys, "frozen", False):
                    exe = sys.executable
                else:
                    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Locazo.exe")
                val = f'"{exe}"'
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, val)
            winreg.CloseKey(key)
        except Exception:
            pass

    def _autostart_enabled(self) -> bool:
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ
            )
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except (FileNotFoundError, OSError):
            return False

    # ── quit ──

    def _quit(self, *_):
        # Stop the RegisterHotKey message loop
        if hasattr(self, "_hotkey_tid"):
            ctypes.windll.user32.PostThreadMessageW(
                self._hotkey_tid, 0x0012, 0, 0  # WM_QUIT
            )
        if self.icon:
            self.icon.stop()


# ── Entry point ──────────────────────────────────────────────────────
if __name__ == "__main__":
    Locazo().run()
