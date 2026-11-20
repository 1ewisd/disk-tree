import os
import time

import pytest
from PySide6.QtWidgets import QApplication

from disktree.ui import Window

MIB = 1024 * 1024


@pytest.fixture
def window(tmp_path):
    app = QApplication.instance() or QApplication([])
    for name, size in (("big/a.bin", 3 * MIB), ("big/inner/b.bin", MIB), ("small/c.bin", 4096), ("top.bin", MIB)):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(size))
    win = Window(str(tmp_path))
    yield app, win, tmp_path
    win.scanner.stop()
    win.ticker.stop()
    win.close()


def settle(app, win, timeout=10):
    end = time.time() + timeout
    while win.busy() and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert not win.busy()


def names(item):
    return [item.child(i).text(0) for i in range(item.childCount())]


def test_scan_sorts_folders_first_biggest_first(window):
    app, win, _ = window
    settle(app, win)
    root = win.tree.topLevelItem(0)
    assert names(root) == ["big", "small", "top.bin"]
    assert "%" in root.child(0).text(1)


def test_expanding_a_folder_lists_its_children(window):
    app, win, _ = window
    settle(app, win)
    big = win.tree.topLevelItem(0).child(0)
    big.setExpanded(True)
    settle(app, win)
    assert names(big) == ["inner", "a.bin"]


def test_folders_only_hides_files(window):
    app, win, _ = window
    settle(app, win)
    win.set_folders_only(True)
    root = win.tree.topLevelItem(0)
    assert root.child(2).isHidden() and not root.child(0).isHidden()


def test_trash_updates_sizes(window, monkeypatch):
    app, win, tmp = window
    settle(app, win)
    root = win.tree.topLevelItem(0)
    before = root.size
    top = root.child(2)
    monkeypatch.setattr("disktree.ui.QMessageBox.question", lambda *a, **k: __import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.Yes)
    monkeypatch.setattr("disktree.ui.trash", lambda path: os.remove(path))
    win.trash_item(top)
    assert root.size < before and "top.bin" not in names(root)
