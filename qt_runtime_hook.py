"""Ensure Qt and its ICU dependencies are discoverable in one-file builds."""

import os
import sys


if sys.platform == "win32":
    extraction_root = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    qt_directory = os.path.join(extraction_root, "PySide6")
    for directory in (extraction_root, qt_directory):
        if os.path.isdir(directory):
            try:
                os.add_dll_directory(directory)
            except (AttributeError, OSError):
                pass
    os.environ["PATH"] = os.pathsep.join(
        path for path in (qt_directory, extraction_root, os.environ.get("PATH", "")) if path
    )
