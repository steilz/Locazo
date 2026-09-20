"""KDE Wayland capture and global shortcuts (no X11 grabs or screen access)."""

import logging
import subprocess
import uuid
from pathlib import Path

from gi.repository import Gio, GLib

log = logging.getLogger("locazo")
PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
REQUEST = "org.freedesktop.portal.Request"


def capture(path: Path, region: bool) -> bool:
    """Run KDE's selector; cancellation produces no output file."""
    # A shared instance can acknowledge DBus activation before saving the image.
    # Own the process so its exit means capture has finished (or was cancelled).
    result = subprocess.run(
        ["spectacle", "--new-instance", "--background", "--nonotify",
         "--region" if region else "--current",
         *(["--release-capture"] if region else []), "--output", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if path.is_file() and path.stat().st_size:
        if result.returncode:
            raise RuntimeError(f"Spectacle failed: {result.stderr.strip()}")
        return True
    if result.returncode:
        log.info("Capture cancelled or unavailable: %s", result.stderr.strip())
    return False


class PortalHotkeys:
    """Register shortcuts through the desktop's user consent dialog."""

    def __init__(self, callbacks, on_status):
        self.callbacks = callbacks
        self.on_status = on_status
        # A private connection avoids prior GTK portal calls locking in an empty app ID.
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        self.bus = Gio.DBusConnection.new_for_address_sync(
            address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
            Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None,
        )
        self.bus.call_sync(
            PORTAL, PORTAL_PATH, "org.freedesktop.host.portal.Registry", "Register",
            GLib.Variant("(sa{sv})", ("io.github.steilz.Locazo", {})),
            None, Gio.DBusCallFlags.NONE, 5000, None,
        )
        self.session = None
        self.pending = {}
        self.stopped = False
        self.signals = []

    def start(self):
        self.signals.append(self.bus.signal_subscribe(
            PORTAL, SHORTCUTS, "Activated", PORTAL_PATH, None,
            Gio.DBusSignalFlags.NONE, self._activated,
        ))
        self.signals.append(self.bus.signal_subscribe(
            PORTAL, "org.freedesktop.portal.Session", "Closed", None, None,
            Gio.DBusSignalFlags.NONE, self._closed,
        ))
        self._request("CreateSession", lambda token: GLib.Variant("(a{sv})", ({
            "handle_token": GLib.Variant("s", token),
            "session_handle_token": GLib.Variant("s", "locazo_" + uuid.uuid4().hex),
        },)), self._created)

    def _request(self, method, parameters, callback):
        # Subscribe before making the call: Response can precede the method reply.
        token = "locazo_" + uuid.uuid4().hex
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        path = f"{PORTAL_PATH}/request/{sender}/{token}"

        def response(_bus, _sender, _path, _iface, _signal, args):
            entry = self.pending.pop(path, None)
            if entry:
                self.bus.signal_unsubscribe(entry)
            if self.stopped:
                return
            code, results = args.unpack()
            if code:
                log.warning("Shortcut permission was cancelled or denied (%s)", code)
                self.on_status("Shortcuts disabled — use tray menu")
                return
            callback(results)

        self.pending[path] = self.bus.signal_subscribe(
            PORTAL, REQUEST, "Response", path, None,
            Gio.DBusSignalFlags.NONE, response,
        )

        def finished(bus, result):
            try:
                bus.call_finish(result)
            except GLib.Error as exc:
                entry = self.pending.pop(path, None)
                if entry:
                    self.bus.signal_unsubscribe(entry)
                if not self.stopped:
                    log.error("Global shortcuts portal: %s", exc)
                    self.on_status("Shortcuts unavailable — use tray menu")

        self.bus.call(PORTAL, PORTAL_PATH, SHORTCUTS, method, parameters(token),
                      GLib.VariantType.new("(o)"), Gio.DBusCallFlags.NONE,
                      -1, None, finished)

    def _created(self, results):
        self.session = results["session_handle"]
        shortcuts = [
            ("region", {"description": GLib.Variant("s", "Capture a region"),
                        "preferred_trigger": GLib.Variant("s", "CTRL+SHIFT+c")}),
            ("fullscreen", {"description": GLib.Variant("s", "Capture current monitor"),
                            "preferred_trigger": GLib.Variant("s", "CTRL+SHIFT+F11")}),
        ]
        self._request("BindShortcuts", lambda token: GLib.Variant(
            "(oa(sa{sv})sa{sv})", (self.session, shortcuts, "", {
                "handle_token": GLib.Variant("s", token),
            })), self._bound)

    def _bound(self, results):
        shortcuts = results.get("shortcuts", [])
        labels = [options.get("trigger_description", key) for key, options in shortcuts]
        self.on_status("Shortcuts: " + ", ".join(labels) if labels
                       else "Shortcuts disabled — use tray menu")
        log.info("Wayland shortcuts registered: %s", labels)

    def _activated(self, _bus, _sender, _path, _iface, _signal, args):
        session, shortcut, _time, _options = args.unpack()
        if session == self.session and not self.stopped:
            callback = self.callbacks.get(shortcut)
            if callback:
                callback()

    def _closed(self, _bus, _sender, path, _iface, _signal, _args):
        if path == self.session and not self.stopped:
            self.session = None
            self.on_status("Shortcuts disconnected — restart Locazo")

    def stop(self):
        self.stopped = True
        for path, subscription in self.pending.items():
            self.bus.signal_unsubscribe(subscription)
            self.bus.call(PORTAL, path, REQUEST, "Close", None, None,
                          Gio.DBusCallFlags.NONE, -1, None, None)
        self.pending.clear()
        for subscription in self.signals:
            self.bus.signal_unsubscribe(subscription)
        self.signals.clear()
        if self.session:
            self.bus.call(PORTAL, self.session, "org.freedesktop.portal.Session",
                          "Close", None, None, Gio.DBusCallFlags.NONE, -1, None, None)
            self.session = None
