"""Launcher. Double-click this file, or use start.bat."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from focusgate.app import main  # noqa: E402

raise SystemExit(main())
