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
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ── Configuration ─────────────────────────────────────────────────────
SAVE_DIR = Path.home() / "Pictures" / "Locazo"
APP_NAME = "Locazo"
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

# ── Win32 Constants ───────────────────────────────────────────────────
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_C = 0x43
VK_ESCAPE = 0x1B
VK_F11 = 0x7A
WM_HOTKEY = 0x0312

HOTKEY_ID_REGION = 1
HOTKEY_ID_FULLSCREEN = 2
HOTKEY_ID_ESC = 3

SHCNE_CREATE = 0x00000002
SHCNE_UPDATEDIR = 0x00001000
SHCNF_PATHW = 0x0005
SHCNF_FLUSH = 0x1000

CF_DIB = 8
GMEM_MOVEABLE = 0x0002

# ── Win32 API Type Definitions ────────────────────────────────────────
_u32 = ctypes.windll.user32
_k32 = ctypes.windll.kernel32
_shell32 = ctypes.windll.shell32
_ole32 = ctypes.windll.ole32

_u32.RegisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.wintypes.UINT, ctypes.wintypes.UINT]
_u32.RegisterHotKey.restype = ctypes.wintypes.BOOL

_u32.UnregisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
_u32.UnregisterHotKey.restype = ctypes.wintypes.BOOL

_u32.GetMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG), ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.UINT]
_u32.GetMessageW.restype = ctypes.wintypes.BOOL

_u32.PostThreadMessageW.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_u32.PostThreadMessageW.restype = ctypes.wintypes.BOOL

_u32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
_u32.OpenClipboard.restype = ctypes.wintypes.BOOL

_u32.EmptyClipboard.argtypes = []
_u32.EmptyClipboard.restype = ctypes.wintypes.BOOL

_u32.SetClipboardData.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.HANDLE]
_u32.SetClipboardData.restype = ctypes.wintypes.HANDLE

_u32.CloseClipboard.argtypes = []
_u32.CloseClipboard.restype = ctypes.wintypes.BOOL

_k32.GlobalAlloc.argtypes = [ctypes.wintypes.UINT, ctypes.c_size_t]
_k32.GlobalAlloc.restype = ctypes.c_void_p

_k32.GlobalLock.argtypes = [ctypes.c_void_p]
_k32.GlobalLock.restype = ctypes.c_void_p

_k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_k32.GlobalUnlock.restype = ctypes.wintypes.BOOL

_k32.GetCurrentThreadId.argtypes = []
_k32.GetCurrentThreadId.restype = ctypes.wintypes.DWORD

_shell32.ILCreateFromPathW.argtypes = [ctypes.wintypes.LPCWSTR]
_shell32.ILCreateFromPathW.restype = ctypes.c_void_p

_shell32.ILFree.argtypes = [ctypes.c_void_p]
_shell32.ILFree.restype = None

_shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.wintypes.UINT, ctypes.c_void_p, ctypes.wintypes.DWORD]
_shell32.SHOpenFolderAndSelectItems.restype = ctypes.HRESULT

_shell32.SHChangeNotify.argtypes = [ctypes.wintypes.LONG, ctypes.wintypes.UINT, ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
_shell32.SHChangeNotify.restype = None

_ole32.CoInitialize.argtypes = [ctypes.c_void_p]
_ole32.CoInitialize.restype = ctypes.HRESULT

_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None


# ── Clipboard ────────────────────────────────────────────────────────
def copy_image_to_clipboard(image: Image.Image):
    """Copy a PIL Image to the Windows clipboard as CF_DIB."""
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]  # Skip 14-byte BMP file header
    buf.close()

    if _u32.OpenClipboard(None):
        _u32.EmptyClipboard()
        h = _k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if h:
            p = _k32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            _k32.GlobalUnlock(h)
            _u32.SetClipboardData(CF_DIB, h)
        _u32.CloseClipboard()


# ── Explorer Integration ─────────────────────────────────────────────
def show_in_explorer(path: Path):
    """Open Explorer with the file selected. Reuses existing windows."""
    # Notify shell so Explorer refreshes its view
    _shell32.SHChangeNotify(SHCNE_CREATE, SHCNF_PATHW | SHCNF_FLUSH, str(path), None)
    _shell32.SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW | SHCNF_FLUSH, str(path.parent), None)

    # Open folder and select file via Shell API
    _ole32.CoInitialize(None)
    try:
        pidl = _shell32.ILCreateFromPathW(str(path))
        if pidl:
            try:
                _shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
            finally:
                _shell32.ILFree(pidl)
    finally:
        _ole32.CoUninitialize()


# ── Tray Icon ────────────────────────────────────────────────────────
def make_tray_icon() -> Image.Image:
    """Generate a tray icon: blue rounded square with white crosshair."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(66, 133, 244))
    cx, cy = 32, 32
    d.line([(cx - 14, cy), (cx + 14, cy)], fill="white", width=2)
    d.line([(cx, cy - 14), (cx, cy + 14)], fill="white", width=2)
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

    def show(self):
        """Capture screen, display overlay, enter mainloop (blocking)."""
        with mss.mss() as sct:
            mon = sct.monitors[0]
            raw = sct.grab(mon)
            self.screenshot = Image.frombytes("RGB", raw.size, raw.rgb)
            self.mon_left = mon["left"]
            self.mon_top = mon["top"]
            self.mon_w = mon["width"]
            self.mon_h = mon["height"]

        self._build_ui()
        self.root.mainloop()

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

        self.photo = ImageTk.PhotoImage(self.screenshot)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        stip = "gray50"
        self.d_top = self.canvas.create_rectangle(
            0, 0, self.mon_w, self.mon_h, fill="black", outline="", stipple=stip
        )
        self.d_bot = self.canvas.create_rectangle(0, 0, 0, 0, fill="black", outline="", stipple=stip)
        self.d_lft = self.canvas.create_rectangle(0, 0, 0, 0, fill="black", outline="", stipple=stip)
        self.d_rgt = self.canvas.create_rectangle(0, 0, 0, 0, fill="black", outline="", stipple=stip)

        self.sel_rect = self.canvas.create_rectangle(0, 0, 0, 0, outline="#00aaff", width=2)
        self.dim_bg = self.canvas.create_rectangle(0, 0, 0, 0, fill="#1a1a2e", outline="")
        self.dim_txt = self.canvas.create_text(
            0, 0, text="", fill="#00aaff", font=("Segoe UI", 10, "bold"), anchor="nw"
        )

        self.sx = self.sy = 0
        self.dragging = False
        self._done = False

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>", lambda _: self._finish(None))

        # ESC via RegisterHotKey on a dedicated thread (no hooks, anti-cheat safe)
        self._esc_thread = threading.Thread(target=self._esc_hotkey_loop, daemon=True)
        self._esc_thread.start()

        self.root.deiconify()
        self.root.focus_force()

    def _esc_hotkey_loop(self):
        """Register ESC as a hotkey and listen on a dedicated thread."""
        self._esc_tid = _k32.GetCurrentThreadId()
        _u32.RegisterHotKey(None, HOTKEY_ID_ESC, 0, VK_ESCAPE)
        try:
            msg = ctypes.wintypes.MSG()
            while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID_ESC:
                    if not self._done:
                        self.root.after(0, lambda: self._finish(None))
                    break
        finally:
            _u32.UnregisterHotKey(None, HOTKEY_ID_ESC)

    def _on_press(self, e):
        self.sx, self.sy = e.x, e.y
        self.dragging = True

    def _on_drag(self, e):
        if not self.dragging:
            return

        x1, y1 = min(self.sx, e.x), min(self.sy, e.y)
        x2, y2 = max(self.sx, e.x), max(self.sy, e.y)

        self.canvas.coords(self.sel_rect, x1, y1, x2, y2)

        w, h = self.mon_w, self.mon_h
        self.canvas.coords(self.d_top, 0, 0, w, y1)
        self.canvas.coords(self.d_bot, 0, y2, w, h)
        self.canvas.coords(self.d_lft, 0, y1, x1, y2)
        self.canvas.coords(self.d_rgt, x2, y1, w, y2)

        pw, ph = x2 - x1, y2 - y1
        label = f"{pw} \u00d7 {ph}"
        self.canvas.itemconfigure(self.dim_txt, text=label)

        tx, ty = x2 + 8, y2 + 8
        if tx + 100 > w:
            tx = x1 - 100
        if ty + 24 > h:
            ty = y1 - 24

        self.canvas.coords(self.dim_txt, tx, ty)
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

    def _finish(self, result):
        """Clean up overlay and deliver result. Safe to call multiple times."""
        if self._done:
            return
        self._done = True
        # Stop ESC hotkey thread
        if hasattr(self, "_esc_tid"):
            _u32.PostThreadMessageW(self._esc_tid, 0x0012, 0, 0)  # WM_QUIT
        self.root.destroy()
        self.callback(result)


# ── Main Application ─────────────────────────────────────────────────
class Locazo:
    """System-tray screenshot application."""

    def __init__(self):
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        self.capturing = False
        self.icon = None

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
        """Listen for global hotkeys via Win32 RegisterHotKey."""
        mods = MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT
        _u32.RegisterHotKey(None, HOTKEY_ID_REGION, mods, VK_C)
        _u32.RegisterHotKey(None, HOTKEY_ID_FULLSCREEN, mods, VK_F11)
        self._hotkey_tid = _k32.GetCurrentThreadId()

        try:
            msg = ctypes.wintypes.MSG()
            while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    if msg.wParam == HOTKEY_ID_REGION:
                        self._region()
                    elif msg.wParam == HOTKEY_ID_FULLSCREEN:
                        self._fullscreen()
        finally:
            _u32.UnregisterHotKey(None, HOTKEY_ID_REGION)
            _u32.UnregisterHotKey(None, HOTKEY_ID_FULLSCREEN)

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
            raw = sct.grab(sct.monitors[1])
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

        try:
            copy_image_to_clipboard(img)
        except Exception:
            pass

        try:
            show_in_explorer(path)
        except Exception:
            pass

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
                if getattr(sys, "frozen", False):
                    exe = sys.executable
                else:
                    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Locazo.exe")
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}"')
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

    def _quit(self, *_):
        if hasattr(self, "_hotkey_tid"):
            _u32.PostThreadMessageW(self._hotkey_tid, 0x0012, 0, 0)
        if self.icon:
            self.icon.stop()


# ── Entry point ──────────────────────────────────────────────────────
if __name__ == "__main__":
    Locazo().run()
