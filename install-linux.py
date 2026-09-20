#!/usr/bin/env python3
"""Install the user launcher. Keep this checkout in its current location."""
import os
from pathlib import Path
import sys

if os.geteuid() == 0:
    raise SystemExit("Run this installer as your desktop user, without sudo.")

from locazo_linux import current_command, write_tray_icon

applications = Path.home() / ".local/share/applications"
applications.mkdir(parents=True, exist_ok=True)
launcher = applications / "io.github.steilz.Locazo.desktop"
launcher.write_text(
    "[Desktop Entry]\nType=Application\nName=Locazo\n"
    "Comment=Local screenshots and clipboard capture\n"
    f"Exec={current_command()}\nIcon={write_tray_icon()}\n"
    "Terminal=false\nCategories=Utility;Graphics;\nStartupNotify=false\n",
    encoding="utf-8",
)
print(f"Installed {launcher}. Start Locazo from your application menu.")
