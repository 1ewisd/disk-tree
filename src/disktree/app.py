"""Application entry point."""
import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import __version__
from .ui import Window

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def _screenshot(app, win, out):
    """Developer helper: expand the biggest folders, wait for the scan, save a PNG."""
    state = {"step": 0}

    def step():
        if win.busy():
            return
        root = win.tree.topLevelItem(0)
        dirs = [root.child(i) for i in range(root.childCount()) if root.child(i).is_dir]
        if state["step"] == 0:
            for d in dirs[:3]:
                d.setExpanded(True)
        elif state["step"] == 1:
            for d in dirs[:3]:
                for i in range(d.childCount()):
                    if d.child(i).is_dir:
                        d.child(i).setExpanded(True)
                        break
        else:
            app.processEvents()
            win.grab().save(out)
            app.quit()
        state["step"] += 1

    t = QTimer()
    t.timeout.connect(step)
    t.start(700)
    return t


def main():
    if "--version" in sys.argv:
        print(f"disktree {__version__}")
        return
    args = [a for a in sys.argv[1:]]
    shot = label = None
    if "--label" in args:
        i = args.index("--label")
        label = args[i + 1]
        del args[i:i + 2]
    if "--screenshot" in args:
        i = args.index("--screenshot")
        shot = args[i + 1]
        del args[i:i + 2]
    root = os.path.realpath(os.path.expanduser(args[0])) if args else "/"
    if not os.path.isdir(root):
        sys.exit(f"disktree: not a folder: {root}")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Disk Tree")
    app.setDesktopFileName("disktree")
    app.setWindowIcon(QIcon(os.path.join(ASSETS, "disktree.svg")))
    win = Window(root, label)
    win.show()
    keep = _screenshot(app, win, shot) if shot else None
    code = app.exec()
    del keep
    sys.exit(code)
