"""Disk Tree main window."""
import os
import shutil
import subprocess
import time

from PySide6.QtCore import QFile, QObject, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMenu,
                               QMessageBox, QPushButton, QStyledItemDelegate, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .scanner import Scanner, human_size

FRAC = Qt.UserRole + 3      # bar fraction for the Size column (-1 = no bar)


def bar_color(f):
    if f >= 0.85:
        return QColor(201, 62, 53)
    if f >= 0.60:
        return QColor(224, 164, 8)
    return QColor(65, 160, 88)


def paint_bar(painter, rect, frac, radius=4):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(127, 127, 127, 50))
    painter.drawRoundedRect(rect, radius, radius)
    fill = bar_color(frac)
    fill.setAlpha(200)
    painter.setBrush(fill)
    w = max(radius * 2 + 1.0, float(rect.width()) * min(frac, 1.0))
    painter.drawRoundedRect(QRectF(float(rect.x()), float(rect.y()), w, float(rect.height())), radius, radius)
    painter.restore()


class SizeBarDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        frac = index.data(FRAC)
        if isinstance(frac, float) and frac >= 0:
            paint_bar(painter, option.rect.adjusted(2, 2, -2, -2), frac)
        super().paint(painter, option, index)


class TItem(QTreeWidgetItem):
    """A file or folder row. Sorts folders first, then biggest first (use Descending)."""

    def __init__(self, text, path, is_dir, size=None):
        super().__init__(text)
        self.path, self.is_dir, self.size = path, is_dir, size
        self.loaded = False
        self.placeholder = False

    def __lt__(self, other):
        return (self.is_dir, self.size or 0) < (other.is_dir, other.size or 0)


class Bridge(QObject):
    """Carries scanner callbacks from worker threads onto the GUI thread."""
    listed = Signal(str, object, object)
    sized = Signal(str, str, object)
    done = Signal(str)


def trash(path):
    """Move to the freedesktop trash. Returns an error string, or None on success."""
    if shutil.which("gio"):
        r = subprocess.run(["gio", "trash", path], capture_output=True, text=True)
        return None if r.returncode == 0 else (r.stderr.strip() or "gio trash failed")
    return None if QFile.moveToTrash(path) else "could not move to the trash"


class Window(QWidget):
    def __init__(self, root, label=None):
        super().__init__()
        self.label = label           # overrides how the root is shown (used for screenshots)
        self.cache = {}
        self.scanner = Scanner(self.cache)
        self.bridge = Bridge()
        self.bridge.listed.connect(self.on_listed)
        self.bridge.sized.connect(self.on_sized)
        self.bridge.done.connect(self.on_done)
        self._pending = {}     # folder path -> item awaiting its scan
        self._items = {}       # path -> item
        self.root = root
        self.folders_only = False
        self.started = 0.0

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Size"])
        self.tree.setUniformRowHeights(True)
        self.tree.setItemDelegateForColumn(1, SizeBarDelegate(self.tree))
        self.tree.itemExpanded.connect(self.on_expand)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self.open_item(it) if not it.is_dir else None)
        h = self.tree.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.Fixed)
        h.resizeSection(1, 190)

        self.up_btn = self._button("Up", self.go_up, "Scan the parent folder (Backspace)")
        self.rescan_btn = self._button("Rescan", self.rebuild, "Forget cached sizes and scan again (F5)")
        self.stop_btn = self._button("Stop", self.stop, "Stop scanning (Esc)")
        self.folder_btn = self._button("Choose folder…", self.choose_folder)
        self.fm_btn = self._button("Open in file manager", lambda: self.open_path(self.root))
        self.only_dirs = QCheckBox("Folders only")
        self.only_dirs.toggled.connect(self.set_folders_only)
        QShortcut(QKeySequence("F5"), self, self.rebuild)
        QShortcut(QKeySequence("Backspace"), self, self.go_up)
        QShortcut(QKeySequence("Escape"), self, self.stop)
        QShortcut(QKeySequence("Delete"), self, self.trash_selected)

        top = QHBoxLayout()
        top.setContentsMargins(8, 8, 8, 0)
        top.setSpacing(6)
        for w in (self.up_btn, self.rescan_btn, self.stop_btn, self.folder_btn, self.fm_btn, self.only_dirs):
            top.addWidget(w)
        top.addStretch(1)
        self.status = QLabel()
        self.status.setContentsMargins(8, 2, 8, 4)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.tree)
        layout.addWidget(self.status)

        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self.update_status)
        self.resize(900, 640)
        self.set_root(root)

    def _button(self, text, slot, tip=""):
        b = QPushButton(text, self)
        b.clicked.connect(slot)
        if tip:
            b.setToolTip(tip)
        return b

    # ── root handling ─────────────────────────────────────────────
    def busy(self):
        return bool(self._pending)

    def set_root(self, path):
        self.root = os.path.abspath(path)
        self.setWindowTitle("Disk Tree — " + self.display(self.root))
        self.up_btn.setEnabled(self.root != os.path.dirname(self.root))
        self.fresh_scan()

    def display(self, path):
        if path == self.root and self.label:
            return self.label
        return "~" if path == os.path.expanduser("~") else path

    def rebuild(self):
        self.cache.clear()
        self.fresh_scan()

    def fresh_scan(self):
        self.scanner.reset()
        self._pending.clear()
        self._items.clear()
        self.tree.clear()
        root_item = TItem([self.display(self.root), "calculating…"], self.root, True)
        try:
            usage = shutil.disk_usage(self.root)
            root_item.setData(1, FRAC, usage.used / usage.total)
        except OSError:
            root_item.setData(1, FRAC, -1.0)
        self._items[self.root] = root_item
        self.tree.addTopLevelItem(root_item)
        self.load(root_item)
        root_item.setExpanded(True)
        self.started = time.monotonic()
        self.ticker.start(250)
        self.update_status()

    def go_up(self):
        parent = os.path.dirname(self.root)
        if parent != self.root:
            self.set_root(parent)

    def choose_folder(self):
        start = self.root if os.path.isdir(self.root) else os.path.expanduser("~")
        picked = QFileDialog.getExistingDirectory(self, "Scan which folder?", start)
        if picked:
            self.set_root(picked)

    def stop(self):
        if not self.busy():
            return
        self.scanner.stop()
        self._pending.clear()
        for it in self._items.values():
            if it.is_dir and it.size is None and it.text(1) == "calculating…":
                it.setText(1, "stopped")
        self.update_status()

    # ── loading ───────────────────────────────────────────────────
    def load(self, item):
        item.loaded = True
        self._pending[item.path] = item
        self.scanner.scan(item.path, self.bridge.listed.emit, self.bridge.sized.emit, self.bridge.done.emit)

    def on_expand(self, item):
        if item.is_dir and not item.loaded:
            self.load(item)

    def _placeholder(self, item):
        dummy = TItem(["(loading…)", ""], "", False)
        dummy.placeholder = True
        item.addChild(dummy)

    def on_listed(self, path, entries, error):
        item = self._pending.get(path)
        if item is None:
            return
        self._clear_children(item)
        if entries is None:
            item.addChild(TItem([f"(not readable: {error})", "—"], "", False))
            return
        for name, child_path, is_dir, size, other_fs in entries:
            if size is None and not is_dir:
                text = "—"
            elif other_fs:
                text = "other filesystem"
            else:
                text = human_size(size) if size is not None else "calculating…"
            child = TItem([name, text], child_path, is_dir and not other_fs, size)
            child.setData(1, FRAC, -1.0)
            child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            if is_dir and not other_fs:
                self._placeholder(child)
            child.setHidden(self.folders_only and not child.is_dir)
            self._items[child_path] = child
            item.addChild(child)

    def _clear_children(self, item):
        for i in reversed(range(item.childCount())):
            c = item.takeChild(i)
            self._forget(c)

    def _forget(self, item):
        self._items.pop(item.path, None)
        for i in range(item.childCount()):
            self._forget(item.child(i))

    def on_sized(self, _parent, child_path, size):
        child = self._items.get(child_path)
        if child is not None:
            child.size = size
            child.setText(1, human_size(size))

    def on_done(self, path):
        item = self._pending.pop(path, None)
        if item is None:
            return
        kids = [item.child(i) for i in range(item.childCount()) if not item.child(i).placeholder]
        total = sum(k.size or 0 for k in kids)
        if item.size is None:
            item.size = total
        if item.parent() is None:                     # sub-folders keep their "size · share" label
            item.setText(1, human_size(item.size))
        self.apply_fractions(item)
        item.sortChildren(1, Qt.DescendingOrder)
        self.update_status()

    def apply_fractions(self, item):
        kids = [item.child(i) for i in range(item.childCount()) if not item.child(i).placeholder]
        total = sum(k.size or 0 for k in kids)
        for k in kids:
            if k.size is None or total <= 0:
                continue
            frac = min(k.size / total, 1.0)
            k.setData(1, FRAC, frac)
            k.setText(1, f"{human_size(k.size)} · {frac:.0%}")

    def set_folders_only(self, on):
        self.folders_only = on
        for it in self._items.values():
            if not it.is_dir and it is not self.tree.topLevelItem(0):
                it.setHidden(on)

    def update_status(self):
        if self.busy():
            self.status.setText(f"Scanning…  {len(self.cache):,} folders measured · "
                                f"{time.monotonic() - self.started:.0f}s")
            self.stop_btn.setEnabled(True)
        else:
            self.ticker.stop()
            self.stop_btn.setEnabled(False)
            root = self.tree.topLevelItem(0)
            try:
                u = shutil.disk_usage(self.root)
                disk = f"   ·   disk {human_size(u.used)} of {human_size(u.total)} used"
            except OSError:
                disk = ""
            self.status.setText(f"{self.display(self.root)}: {human_size(root.size) if root and root.size is not None else '—'}{disk}")

    # ── actions ───────────────────────────────────────────────────
    def open_path(self, path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_item(self, item):
        if item.path:
            self.open_path(item.path)

    def selected(self):
        it = self.tree.currentItem()
        return it if it is not None and not it.placeholder and it.path else None

    def context_menu(self, pos):
        it = self.tree.itemAt(pos)
        if it is None or it.placeholder or not it.path:
            return
        self.tree.setCurrentItem(it)
        m = QMenu(self)
        m.addAction("Open", lambda: self.open_item(it))
        if not it.is_dir:
            m.addAction("Show in file manager", lambda: self.open_path(os.path.dirname(it.path)))
        if it.is_dir:
            m.addAction("Scan this folder as root", lambda: self.set_root(it.path))
            m.addAction("Rescan this folder", lambda: self.rescan_item(it))
        m.addAction("Copy path", lambda: QGuiApplication.clipboard().setText(it.path))
        m.addSeparator()
        if it is not self.tree.topLevelItem(0):
            m.addAction("Move to Trash…", lambda: self.trash_item(it))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def trash_selected(self):
        it = self.selected()
        if it is not None and it is not self.tree.topLevelItem(0):
            self.trash_item(it)

    def trash_item(self, it):
        what = "folder and everything in it" if it.is_dir else "file"
        if QMessageBox.question(self, "Move to Trash",
                                f"Move this {what} to the Trash?\n\n{it.path}\n({human_size(it.size)})") != QMessageBox.Yes:
            return
        err = trash(it.path)
        if err:
            QMessageBox.warning(self, "Move to Trash", f"Couldn't move it to the Trash:\n{err}")
            return
        size = it.size or 0
        parent = it.parent()
        parent.removeChild(it)
        self._forget(it)
        for p in [k for k in self.cache if k == it.path or k.startswith(it.path + os.sep)]:
            del self.cache[p]
        node = parent                                   # shrink every ancestor by what we removed
        while node is not None:
            if node.size is not None:
                node.size = max(node.size - size, 0)
                self.cache[node.path] = node.size
                if node.parent() is None:
                    node.setText(1, human_size(node.size))
            node = node.parent()
        self.refresh_labels(parent)
        self.update_status()

    def refresh_labels(self, item):
        """After a size change: redo the percentage labels up the chain."""
        node = item
        while node is not None:
            self.apply_fractions(node)
            node.sortChildren(1, Qt.DescendingOrder)
            node = node.parent()

    def rescan_item(self, it):
        for p in [k for k in self.cache if k == it.path or k.startswith(it.path + os.sep)]:
            del self.cache[p]
        it.size = None
        it.setText(1, "calculating…")
        self._clear_children(it)
        self._placeholder(it)
        it.loaded = False
        self.load(it)
        it.setExpanded(True)
        self.ticker.start(250)
