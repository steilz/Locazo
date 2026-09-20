import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from gi.repository import GLib
import locazo_linux as linux
import locazo_wayland as wayland


class CaptureTests(unittest.TestCase):
    def test_region_uses_spectacle_and_checks_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.png"
            def run(command, **kwargs):
                self.assertIn("--region", command)
                self.assertIn("--new-instance", command)
                self.assertIn("--release-capture", command)
                self.assertEqual(command[-2:], ["--output", str(path)])
                path.write_bytes(b"image")
                return subprocess.CompletedProcess(command, 0, stderr="")
            with patch.object(wayland.subprocess, "run", side_effect=run):
                self.assertTrue(wayland.capture(path, True))

    def test_cancel_does_not_create_image(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.png"
            with patch.object(wayland.subprocess, "run", return_value=Mock(returncode=1, stderr="cancelled")):
                self.assertFalse(wayland.capture(path, True))
                self.assertFalse(path.exists())

    def test_fullscreen_keeps_current_monitor_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.png"
            with patch.object(wayland.subprocess, "run", return_value=Mock(returncode=0, stderr="")) as run:
                wayland.capture(path, False)
                self.assertIn("--current", run.call_args.args[0])
                self.assertNotIn("--region", run.call_args.args[0])

    def test_clipboard_backend_and_mime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.jpg"
            path.write_bytes(b"image")
            app = object.__new__(linux.Locazo)
            for is_wayland, executable in [(True, "wl-copy"), (False, "xclip")]:
                with patch.object(linux, "IS_WAYLAND", is_wayland), patch.object(linux.subprocess, "run") as run:
                    app.copy_to_clipboard(path)
                    self.assertEqual(run.call_args.args[0][0], executable)
                    self.assertIn("image/jpeg", run.call_args.args[0])

    def test_cancel_releases_capture_lock_without_saving(self):
        app = object.__new__(linux.Locazo)
        app.capturing = Mock()
        app.new_path = Mock()
        with patch.object(wayland, "capture", return_value=False):
            app._capture_wayland(True)
        app.new_path.assert_not_called()
        app.capturing.release.assert_called_once()

    def test_success_saves_copies_and_opens_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.png"
            app = object.__new__(linux.Locazo)
            app.capturing = Mock()
            app.new_path = Mock(return_value=path)
            app.copy_to_clipboard = Mock()
            app.open_folder = Mock()
            def capture(temporary, region):
                linux.Image.new("RGB", (10, 10)).save(temporary)
                return True
            with patch.object(wayland, "capture", side_effect=capture):
                app._capture_wayland(True)
            self.assertTrue(path.is_file())
            app.copy_to_clipboard.assert_called_once_with(path)
            app.open_folder.assert_called_once()
            app.capturing.release.assert_called_once()

    def test_folder_opens_with_desktop_default_handler(self):
        app = object.__new__(linux.Locazo)
        with patch.object(linux.GLib, "idle_add", side_effect=lambda fn: fn()), patch.object(linux.Gio.AppInfo, "launch_default_for_uri") as launch:
            app.open_folder()
            launch.assert_called_once_with(linux.SAVE_DIR.as_uri(), None)

    def test_wayland_requires_kde(self):
        with patch.object(linux, "IS_WAYLAND", True), patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            with self.assertRaisesRegex(SystemExit, "KDE"):
                linux.ensure_runtime()


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.bus = Mock()
        self.bus.get_unique_name.return_value = ":1.42"
        self.bus.signal_subscribe.return_value = 7
        self.status = Mock()
        self.callback = Mock()
        with patch.object(wayland.Gio, "dbus_address_get_for_bus_sync", return_value="unix:path=/test"), patch.object(wayland.Gio.DBusConnection, "new_for_address_sync", return_value=self.bus):
            self.portal = wayland.PortalHotkeys({"region": self.callback}, self.status)

    def test_registers_app_identity_before_using_portals(self):
        args = self.bus.call_sync.call_args.args
        self.assertEqual(args[2:4], ("org.freedesktop.host.portal.Registry", "Register"))
        self.assertEqual(args[4].unpack()[0], "io.github.steilz.Locazo")

    def test_subscribes_before_request_and_handles_early_response(self):
        order = []
        def subscribe(*args):
            order.append("subscribe")
            self.response = args[-1]
            return 7
        def call(*args):
            order.append("call")
            self.response(None, None, None, None, None,
                          GLib.Variant("(ua{sv})", (0, {})))
        self.bus.signal_subscribe.side_effect = subscribe
        self.bus.call.side_effect = call
        done = Mock()
        self.portal._request("CreateSession", lambda token: GLib.Variant("(a{sv})", ({},)), done)
        self.assertEqual(order, ["subscribe", "call"])
        done.assert_called_once_with({})
        self.assertFalse(self.portal.pending)

    def test_permission_denied_keeps_tray_usable(self):
        done = Mock()
        self.portal._request("CreateSession", lambda token: GLib.Variant("(a{sv})", ({},)), done)
        response = self.bus.signal_subscribe.call_args.args[-1]
        response(None, None, None, None, None, GLib.Variant("(ua{sv})", (1, {})))
        done.assert_not_called()
        self.assertIn("tray menu", self.status.call_args.args[0])

    def test_only_own_session_can_activate_shortcuts(self):
        self.portal.session = "/session/ours"
        for session in ("/session/other", "/session/ours"):
            self.portal._activated(None, None, None, None, None,
                                   GLib.Variant("(osta{sv})", (session, "region", 1, {})))
        self.callback.assert_called_once()

    def test_stop_closes_pending_requests_and_session(self):
        self.portal.pending["/request/pending"] = 7
        self.portal.session = "/session/ours"
        self.portal.stop()
        self.assertFalse(self.portal.pending)
        self.assertIsNone(self.portal.session)
        self.assertEqual(self.bus.call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
