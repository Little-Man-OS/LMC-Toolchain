"""Starts the LMC IDE. The window code lives in gui.py so a missing Tk gives a clear message."""
import os
import sys


def _load_gui():
    if __package__:
        from . import gui
    else:                    # run as a plain script, not as part of the package
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from lmc import gui
    return gui


def main(argv=None):
    try:
        gui = _load_gui()
    except ImportError as e:
        if "tkinter" in str(e).lower():
            print("The IDE needs Tk, which this Python does not have.\n"
                  "  Windows and macOS: install Python from python.org (it includes Tk).\n"
                  "  Ubuntu or Debian: sudo apt install python3-tk\n"
                  "  Fedora: sudo dnf install python3-tkinter", file=sys.stderr)
            return 1
        raise
    return gui.main(argv)


if __name__ == "__main__":
    sys.exit(main())
