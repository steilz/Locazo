"""
Locazo - Local Screenshot Tool for Linux Mint/X11.

A lightweight, local-only Gyazo alternative. Same workflow as the Windows
build (locazo.py), reimplemented for X11 desktops: region selection is
delegated to the native gnome-screenshot selector, the tray lives in an
Ayatana AppIndicator, and global hotkeys are grabbed via python-xlib.

Hotkeys:
  Ctrl+Shift+C    - Region capture
  Ctrl+Shift+F11  - Fullscreen capture
"""

import fcntl
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import gi
from PIL import Image, ImageDraw

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator
from gi.repository import Gtk


APP_NAME = "Locazo"
SAVE_DIR = Path.home() / "Pictures" / "Locazo"
AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "locazo.desktop"
LOCK_FILE = Path(tempfile.gettempdir()) / "locazo.lock"
ICON_FILE = Path.home() / ".cache" / "locazo" / "locazo.png"
LOG_FILE = SAVE_DIR / "locazo.log"

JPEG_THRESHOLD = 1_000_000  # bytes; convert PNG -> JPEG above this (like Gyazo)
JPEG_QUALITY = 90

HOTKEY_REGION = "region"
HOTKEY_FULLSCREEN = "fullscreen"

REQUIRED_COMMANDS = ("gnome-screenshot", "xclip", "xdg-open")

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

    missing = [command for command in REQUIRED_COMMANDS if shutil.which(command) is None]
    if missing:
        packages = {
            "gnome-screenshot": "gnome-screenshot",
            "xclip": "xclip",
            "xdg-open": "xdg-utils",
        }
        apt_packages = " ".join(packages[command] for command in missing)
        raise SystemExit(f"Missing system packages. Install them with: sudo apt install {apt_packages}")


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

    def capture_region(self, *_):
        threading.Thread(target=self.capture, args=(True,), daemon=True).start()

    def capture_fullscreen(self, *_):
        threading.Thread(target=self.capture, args=(False,), daemon=True).start()

    def capture(self, area: bool) -> None:
        # A non-blocking lock guards against region and fullscreen captures
        # overlapping (e.g. a hotkey fired mid-capture).
        if not self.capturing.acquire(blocking=False):
            return

        try:
            path = self.new_path()
            command = ["gnome-screenshot", "--file", str(path)]
            if area:
                command.insert(1, "--area")

            result = subprocess.run(command, check=False)
            if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
                # Cancelled selection or a failed grab -- not an error worth a stack trace.
                path.unlink(missing_ok=True)
                if result.returncode not in (0, 1):
                    log.warning("gnome-screenshot exited with code %s", result.returncode)
                return

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
