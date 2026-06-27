"""
Locazo - Local Screenshot Tool for Linux Mint/X11.

A lightweight, local-only Gyazo alternative. Same workflow as the Windows
build (locazo.py), reimplemented natively for X11 desktops: the tray lives in
an Ayatana AppIndicator, global hotkeys are grabbed via python-xlib, and region
selection uses a GTK/Cairo overlay that mirrors the smooth Windows selector —
the screen is grabbed once, shown pre-darkened, and the bright original shows
through only inside the live selection (no per-frame full redraws).

Hotkeys:
  Ctrl+Shift+C    - Region capture
  Ctrl+Shift+F11  - Fullscreen capture
"""

import fcntl
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import cairo
import gi
from PIL import Image, ImageDraw

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk


APP_NAME = "Locazo"
SAVE_DIR = Path.home() / "Pictures" / "Locazo"
AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "locazo.desktop"
LOCK_FILE = Path(tempfile.gettempdir()) / "locazo.lock"
ICON_FILE = Path.home() / ".cache" / "locazo" / "locazo.png"
LOG_FILE = SAVE_DIR / "locazo.log"

JPEG_THRESHOLD = 1_000_000  # bytes; convert PNG -> JPEG above this (like Gyazo)
JPEG_QUALITY = 90
DIM_ALPHA = 0.6  # opacity of the black wash over the un-selected area
SEL_RGB = (0.0, 0.667, 1.0)  # selection outline / label colour (#00aaff)
MIN_SELECTION = 5  # px; smaller drags are treated as a cancel

HOTKEY_REGION = "region"
HOTKEY_FULLSCREEN = "fullscreen"

REQUIRED_COMMANDS = ("xclip", "xdg-open")

log = logging.getLogger("locazo")


def configure_logging() -> None:
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError:
        # If the log file can't be opened, keep going with stderr only.
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def ensure_runtime() -> None:
    if os.environ.get("XDG_SESSION_TYPE") != "x11" or not os.environ.get("DISPLAY"):
        raise SystemExit("Locazo requires a Linux Mint X11 session.")

    missing = [command for command in REQUIRED_COMMANDS if _which(command) is None]
    if missing:
        packages = {"xclip": "xclip", "xdg-open": "xdg-utils"}
        apt_packages = " ".join(packages[command] for command in missing)
        raise SystemExit(f"Missing system packages. Install them with: sudo apt install {apt_packages}")


def _which(command: str) -> str | None:
    import shutil

    return shutil.which(command)


def acquire_single_instance_lock() -> None:
    lock_handle = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)

    globals()["_lock_handle"] = lock_handle


def quote_desktop_exec(value: str) -> str:
    if not any(char.isspace() for char in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def current_command() -> str:
    script = Path(__file__).resolve()
    return f"{quote_desktop_exec('/usr/bin/python3')} {quote_desktop_exec(str(script))}"


def write_tray_icon() -> Path:
    ICON_FILE.parent.mkdir(parents=True, exist_ok=True)
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(66, 133, 244))
    cx, cy = 32, 32
    draw.line([(cx - 14, cy), (cx + 14, cy)], fill="white", width=2)
    draw.line([(cx, cy - 14), (cx, cy + 14)], fill="white", width=2)
    draw.ellipse([(cx - 5, cy - 5), (cx + 5, cy + 5)], outline="white", width=2)
    img.save(ICON_FILE, "PNG")
    return ICON_FILE


class X11Hotkeys(threading.Thread):
    def __init__(self, callbacks: dict[str, Callable[[], None]]):
        super().__init__(daemon=True)
        from Xlib import X, XK, display, error

        self.X = X
        self.XK = XK
        self.display_module = display
        self.error_module = error
        self.callbacks = callbacks
        self.stop_event = threading.Event()
        self.display = None
        self.root = None
        self.grabs: list[tuple[int, int]] = []
        self.keycodes: dict[str, int] = {}

    def start(self) -> None:
        try:
            self.display = self.display_module.Display()
            self.root = self.display.screen().root
            self.keycodes = {
                HOTKEY_REGION: self._keycode("c"),
                HOTKEY_FULLSCREEN: self._keycode("F11"),
            }

            modifiers = self.X.ControlMask | self.X.ShiftMask
            ignored_modifiers = (0, self.X.LockMask, self.X.Mod2Mask, self.X.LockMask | self.X.Mod2Mask)

            for keycode in self.keycodes.values():
                for ignored in ignored_modifiers:
                    mask = modifiers | ignored
                    self.root.grab_key(keycode, mask, True, self.X.GrabModeAsync, self.X.GrabModeAsync)
                    self.grabs.append((keycode, mask))

            self.display.sync()
        except self.error_module.BadAccess:
            log.error("Hotkeys are already registered by another application.")
            raise SystemExit("Locazo hotkeys are already registered by another application.")

        super().start()

    def run(self):
        try:
            while not self.stop_event.is_set():
                while self.display.pending_events():
                    event = self.display.next_event()
                    if event.type == self.X.KeyPress:
                        self._handle_keypress(event)
                time.sleep(0.05)
        finally:
            self._release()

    def _keycode(self, name: str) -> int:
        return self.display.keysym_to_keycode(self.XK.string_to_keysym(name))

    def _handle_keypress(self, event) -> None:
        state = event.state & ~(self.X.LockMask | self.X.Mod2Mask)
        if state != self.X.ControlMask | self.X.ShiftMask:
            return

        if event.detail == self.keycodes[HOTKEY_REGION]:
            self.callbacks[HOTKEY_REGION]()
        elif event.detail == self.keycodes[HOTKEY_FULLSCREEN]:
            self.callbacks[HOTKEY_FULLSCREEN]()

    def stop(self) -> None:
        self.stop_event.set()

    def _release(self) -> None:
        for keycode, mask in self.grabs:
            self.root.ungrab_key(keycode, mask)
        self.display.sync()
        self.display.close()


class RegionOverlay:
    """Fullscreen GTK overlay for smooth interactive region selection.

    A pre-darkened copy of the grabbed screen is painted as a static
    background; the bright original shows through only inside the live
    selection via a Cairo clip. Only the changed region is invalidated per
    motion event, so dragging stays smooth even across multiple monitors.

    ``on_result`` is invoked (on the GTK main thread) with a cropped
    GdkPixbuf on success, or ``None`` on cancel.
    """

    def __init__(self, pixbuf: GdkPixbuf.Pixbuf, origin: tuple[int, int], on_result: Callable):
        self.pixbuf = pixbuf
        self.ox, self.oy = origin
        self.w = pixbuf.get_width()
        self.h = pixbuf.get_height()
        self.on_result = on_result

        self.bright = Gdk.cairo_surface_create_from_pixbuf(pixbuf, 1, None)
        self.dim = Gdk.cairo_surface_create_from_pixbuf(pixbuf, 1, None)
        ctx = cairo.Context(self.dim)
        ctx.set_source_rgba(0, 0, 0, DIM_ALPHA)
        ctx.paint()

        self.start: tuple[float, float] | None = None
        self.cur: tuple[float, float] | None = None
        self.prev_damage: tuple[int, int, int, int] | None = None
        self.done = False
        self._seat = None

        self._build()

    def _build(self) -> None:
        self.win = Gtk.Window()
        self.win.set_decorated(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_keep_above(True)
        self.win.set_app_paintable(True)
        self.win.set_resizable(False)
        self.win.set_can_focus(True)
        self.win.move(self.ox, self.oy)
        self.win.set_default_size(self.w, self.h)

        self.area = Gtk.DrawingArea()
        self.area.set_size_request(self.w, self.h)
        self.area.set_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_press)
        self.area.connect("button-release-event", self._on_release)
        self.area.connect("motion-notify-event", self._on_motion)
        self.win.add(self.area)

        self.win.connect("key-press-event", self._on_key)
        self.win.connect("realize", self._on_realize)
        self.win.show_all()

    def _on_realize(self, *_):
        gwin = self.win.get_window()
        display = self.win.get_display()
        cursor = Gdk.Cursor.new_from_name(display, "crosshair")
        seat = display.get_default_seat()
        try:
            seat.grab(gwin, Gdk.SeatCapabilities.ALL, True, cursor, None, None)
            self._seat = seat
        except Exception:
            # Without an explicit grab the window still gets events while
            # focused; just make sure it has the focus and the right cursor.
            log.warning("Could not grab input seat for the selection overlay.")
            if gwin is not None:
                gwin.set_cursor(cursor)
        self.win.present()

    # ── geometry helpers ──────────────────────────────────────────────
    def _norm(self) -> tuple[int, int, int, int]:
        sx, sy = self.start
        cx, cy = self.cur
        x1, x2 = sorted((sx, cx))
        y1, y2 = sorted((sy, cy))
        x1 = max(0, int(x1))
        y1 = max(0, int(y1))
        x2 = min(self.w, int(x2))
        y2 = min(self.h, int(y2))
        return x1, y1, x2, y2

    def _invalidate(self) -> None:
        if self.start is None or self.cur is None:
            self.area.queue_draw()
            return
        x1, y1, x2, y2 = self._norm()
        margin = 170  # cover the outline and the dimension label
        nx = max(0, x1 - margin)
        ny = max(0, y1 - margin)
        nxr = min(self.w, x2 + margin)
        nyb = min(self.h, y2 + margin)
        damage = (nx, ny, nxr - nx, nyb - ny)

        if self.prev_damage:
            px, py, pw, ph = self.prev_damage
            ux, uy = min(nx, px), min(ny, py)
            uxr, uyb = max(nxr, px + pw), max(nyb, py + ph)
            self.area.queue_draw_area(ux, uy, uxr - ux, uyb - uy)
        else:
            self.area.queue_draw_area(*damage)
        self.prev_damage = damage

    # ── event handlers ────────────────────────────────────────────────
    def _on_press(self, _area, event):
        if event.button == 3:  # right-click cancels
            self._finish(None)
            return True
        self.start = (event.x, event.y)
        self.cur = (event.x, event.y)
        self._invalidate()
        return True

    def _on_motion(self, _area, event):
        if self.start is None:
            return False
        self.cur = (event.x, event.y)
        self._invalidate()
        return True

    def _on_release(self, _area, event):
        if event.button != 1 or self.start is None:
            return False
        self.cur = (event.x, event.y)
        x1, y1, x2, y2 = self._norm()
        if (x2 - x1) > MIN_SELECTION and (y2 - y1) > MIN_SELECTION:
            crop = GdkPixbuf.Pixbuf.new_subpixbuf(self.pixbuf, x1, y1, x2 - x1, y2 - y1)
            self._finish(crop)
        else:
            self._finish(None)
        return True

    def _on_key(self, _win, event):
        if event.keyval == Gdk.KEY_Escape:
            self._finish(None)
            return True
        return False

    def _on_draw(self, _area, cr):
        cr.set_source_surface(self.dim, 0, 0)
        cr.paint()

        if self.start is None or self.cur is None:
            return False

        x1, y1, x2, y2 = self._norm()
        if x2 - x1 < 1 or y2 - y1 < 1:
            return False

        cr.save()
        cr.rectangle(x1, y1, x2 - x1, y2 - y1)
        cr.clip()
        cr.set_source_surface(self.bright, 0, 0)
        cr.paint()
        cr.restore()

        cr.set_source_rgb(*SEL_RGB)
        cr.set_line_width(2)
        cr.rectangle(x1 + 1, y1 + 1, max(0, x2 - x1 - 2), max(0, y2 - y1 - 2))
        cr.stroke()

        self._draw_label(cr, x1, y1, x2, y2)
        return False

    def _draw_label(self, cr, x1, y1, x2, y2) -> None:
        text = f"{x2 - x1} × {y2 - y1}"
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(13)
        ext = cr.text_extents(text)

        pad = 4
        tx = x2 + 8
        ty = y2 + 8 + ext.height
        if tx + ext.width + pad > self.w:
            tx = max(0, x1 - ext.width - 8)
        if ty + pad > self.h:
            ty = max(ext.height, y1 - 8)

        cr.set_source_rgba(0.10, 0.10, 0.18, 0.85)
        cr.rectangle(tx - pad, ty - ext.height - pad, ext.width + 2 * pad, ext.height + 2 * pad)
        cr.fill()

        cr.set_source_rgb(*SEL_RGB)
        cr.move_to(tx, ty)
        cr.show_text(text)

    def _finish(self, result) -> None:
        if self.done:
            return
        self.done = True
        if self._seat is not None:
            try:
                self._seat.ungrab()
            except Exception:
                pass
        self.win.destroy()
        self.on_result(result)


class Locazo:
    def __init__(self):
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        self.capturing = threading.Lock()
        self.hotkeys: X11Hotkeys | None = None
        self.indicator = None
        self.autostart_item: Gtk.CheckMenuItem | None = None

    def run(self) -> None:
        self.hotkeys = X11Hotkeys(
            {
                HOTKEY_REGION: self.capture_region,
                HOTKEY_FULLSCREEN: self.capture_fullscreen,
            }
        )
        self.hotkeys.start()

        self.indicator = AppIndicator.Indicator.new(
            APP_NAME,
            str(write_tray_icon()),
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self.build_menu())
        log.info("Locazo started; saving to %s", SAVE_DIR)
        Gtk.main()

    def build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        self.add_menu_item(menu, "Region capture    Ctrl+Shift+C", self.capture_region)
        self.add_menu_item(menu, "Fullscreen    Ctrl+Shift+F11", self.capture_fullscreen)
        menu.append(Gtk.SeparatorMenuItem())
        self.add_menu_item(menu, "Open folder", self.open_folder)
        menu.append(Gtk.SeparatorMenuItem())

        self.autostart_item = Gtk.CheckMenuItem(label="Autostart")
        self.autostart_item.set_active(self.autostart_enabled())
        self.autostart_item.connect("activate", self.toggle_autostart)
        menu.append(self.autostart_item)

        menu.append(Gtk.SeparatorMenuItem())
        self.add_menu_item(menu, "Quit", self.quit)

        menu.show_all()
        return menu

    def add_menu_item(self, menu: Gtk.Menu, label: str, callback: Callable) -> None:
        item = Gtk.MenuItem(label=label)
        item.connect("activate", callback)
        menu.append(item)

    # ── capture orchestration (all overlay work runs on the GTK thread) ─
    def capture_region(self, *_):
        GLib.idle_add(self._begin_region)

    def capture_fullscreen(self, *_):
        GLib.idle_add(self._begin_fullscreen)

    def _begin_region(self) -> bool:
        if not self.capturing.acquire(blocking=False):
            return False
        try:
            pixbuf, origin = self._grab_root()
            RegionOverlay(pixbuf, origin, self._on_region_result)
        except Exception:
            log.exception("Region capture failed to start")
            self.capturing.release()
        return False

    def _on_region_result(self, pixbuf) -> None:
        if pixbuf is None:
            self.capturing.release()
            return
        threading.Thread(target=self._save_and_release, args=(pixbuf,), daemon=True).start()

    def _begin_fullscreen(self) -> bool:
        if not self.capturing.acquire(blocking=False):
            return False
        try:
            pixbuf = self._grab_primary()
            threading.Thread(target=self._save_and_release, args=(pixbuf,), daemon=True).start()
        except Exception:
            log.exception("Fullscreen capture failed to start")
            self.capturing.release()
        return False

    def _grab_root(self) -> tuple[GdkPixbuf.Pixbuf, tuple[int, int]]:
        root = Gdk.get_default_root_window()
        x, y, w, h = root.get_geometry()
        pixbuf = Gdk.pixbuf_get_from_window(root, x, y, w, h)
        return pixbuf, (x, y)

    def _grab_primary(self) -> GdkPixbuf.Pixbuf:
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geom = monitor.get_geometry()
        root = Gdk.get_default_root_window()
        return Gdk.pixbuf_get_from_window(root, geom.x, geom.y, geom.width, geom.height)

    def _save_and_release(self, pixbuf: GdkPixbuf.Pixbuf) -> None:
        try:
            path = self.new_path()
            pixbuf.savev(str(path), "png", [], [])
            final_path = self.convert_large_png(path)
            self.copy_to_clipboard(final_path)
            self.open_folder()
            log.info("Captured %s", final_path.name)
        except Exception:
            log.exception("Capture failed")
        finally:
            self.capturing.release()

    def new_path(self) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return SAVE_DIR / f"Locazo_{timestamp}.png"

    def convert_large_png(self, path: Path) -> Path:
        if path.stat().st_size <= JPEG_THRESHOLD:
            return path

        jpg = path.with_suffix(".jpg")
        with Image.open(path) as image:
            image.convert("RGB").save(jpg, "JPEG", quality=JPEG_QUALITY)
        path.unlink()
        return jpg

    def copy_to_clipboard(self, path: Path) -> None:
        mime_type = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        with path.open("rb") as file:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-target", mime_type, "-i"],
                stdin=file,
                check=True,
            )

    def open_folder(self, *_):
        subprocess.Popen(
            ["xdg-open", str(SAVE_DIR)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def toggle_autostart(self, item: Gtk.CheckMenuItem):
        if not item.get_active():
            AUTOSTART_FILE.unlink(missing_ok=True)
            return

        AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART_FILE.write_text(
            "\n".join(
                [
                    "[Desktop Entry]",
                    "Type=Application",
                    f"Name={APP_NAME}",
                    f"Exec={current_command()}",
                    "Terminal=false",
                    "X-GNOME-Autostart-enabled=true",
                    "Comment=Local screenshot tool",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def autostart_enabled(self) -> bool:
        return AUTOSTART_FILE.exists()

    def quit(self, *_):
        if self.hotkeys:
            self.hotkeys.stop()
        Gtk.main_quit()


if __name__ == "__main__":
    configure_logging()
    ensure_runtime()
    acquire_single_instance_lock()
    Locazo().run()
