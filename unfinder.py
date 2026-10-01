# /// script
# requires-python = ">=3.10"
# dependencies = ["PySide6>=6.6"]
# ///
"""unfinder: a Finder-style file explorer for macOS, built with PySide6.

Run from source:  uv run unfinder.py [folder]
Build the app:    ./build.sh   (creates dist/unfinder.app)
"""

from __future__ import annotations

import html
import itertools
import json
import shlex
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import shiboken6
from PySide6.QtCore import (
    QCollator, QDir, QFileInfo, QFileSystemWatcher, QMimeData, QMimeDatabase, QModelIndex,
    QItemSelectionModel, QObject, QPersistentModelIndex, QProcess, QProcessEnvironment, QRect, QSettings, QSize, QTimer, QSortFilterProxyModel, QStorageInfo, Qt, QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QDesktopServices, QFont, QFontDatabase, QGuiApplication, QIcon,
    QImage, QImageReader, QKeySequence, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QCompleter, QDialog,
    QDialogButtonBox, QFileDialog, QFileIconProvider, QFileSystemModel, QFormLayout, QFrame,
    QGridLayout, QInputDialog, QPushButton, QRadioButton, QTableWidget, QTableWidgetItem,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QSplitter,
    QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTabWidget, QToolButton, QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

APP_NAME = "unfinder"
# Tests set UNFINDER_SETTINGS so they never touch your real settings.
SETTINGS_SCOPE = os.environ.get("UNFINDER_SETTINGS", "unfinder")
LEGACY_SETTINGS = ("Pyxplorer", "Pyxplorer")  # the app's previous name


def resource_path(name: str) -> str:
    """Find a bundled file both when running from source and inside the built .app."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def migrate_legacy_settings(settings: QSettings) -> None:
    """Carry settings over from when the app was called Pyxplorer (first launch only)."""
    if settings.allKeys() or "UNFINDER_SETTINGS" in os.environ:
        return
    old = QSettings(*LEGACY_SETTINGS)
    for key in old.allKeys():
        settings.setValue(key, old.value(key))
    settings.sync()
HOME = str(Path.home())

STYLE = """
#pane { border: 1px solid transparent; border-radius: 6px; }
#pane[active="true"] { border: 1px solid palette(highlight); }
QTreeView { border: none; }
"""


# ---------------------------------------------------------------- helpers

def human_size(n: float) -> str:
    """Format a byte count the way Finder does (base 1000)."""
    if n < 1000:
        return f"{int(n)} bytes"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1000
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}"
    return ""


def display_name(path: str) -> str:
    if path == "/":
        return QStorageInfo("/").displayName() or "Macintosh HD"
    return os.path.basename(path.rstrip("/")) or path


def unique_dest(dest_dir: Path, name: str) -> Path:
    """Return dest_dir/name, or a Finder-style 'name copy N' variant if taken."""
    target = dest_dir / name
    if not os.path.lexists(target):
        return target
    if target.is_dir():
        stem, suffix = name, ""
    else:
        stem, suffix = Path(name).stem, Path(name).suffix
    for i in itertools.count(1):
        label = " copy" if i == 1 else f" copy {i}"
        cand = dest_dir / f"{stem}{label}{suffix}"
        if not os.path.lexists(cand):
            return cand
    raise AssertionError("unreachable")


def parse_path(text: str) -> str | None:
    """Turn typed/pasted text (quoted, ~, file://, backslash-escaped) into an existing path."""
    t = text.strip()
    if not t or "\n" in t:
        return None
    if len(t) > 1 and t[0] == t[-1] and t[0] in "'\"":
        t = t[1:-1]
    if t.startswith("file://"):
        t = QUrl(t).toLocalFile()
    for cand in (t, t.replace("\\ ", " ")):
        cand = os.path.abspath(os.path.expanduser(cand))
        if os.path.lexists(cand):
            return cand
    return None


_completer_model: QFileSystemModel | None = None


def path_completer(parent) -> QCompleter:
    global _completer_model
    if _completer_model is None:
        _completer_model = QFileSystemModel(QApplication.instance())
        _completer_model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot)
        _completer_model.setRootPath("")
    completer = QCompleter(_completer_model, parent)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    return completer


def move_to_trash(path: str) -> bool:
    from PySide6.QtCore import QFile
    res = QFile.moveToTrash(path)
    return bool(res[0] if isinstance(res, tuple) else res)


class FileOps(QObject):
    """Runs copy/move jobs on a background thread so the UI stays responsive."""

    done = Signal(str, list)  # summary, errors

    def start(self, op: str, sources: list[str], dest_dir: str) -> None:
        threading.Thread(target=self._run, args=(op, list(sources), Path(dest_dir)),
                         daemon=True).start()

    def _run(self, op: str, sources: list[str], dest_dir: Path) -> None:
        errors: list[str] = []
        count = 0
        for s in sources:
            src = Path(s)
            try:
                rd = dest_dir.resolve()
                if op == "move" and src.parent.resolve() == rd:
                    continue  # already there
                if src.is_dir() and not src.is_symlink():
                    rs = src.resolve()
                    if rd == rs or rs in rd.parents:
                        raise OSError("can't put a folder inside itself")
                target = unique_dest(dest_dir, src.name)
                if op == "move":
                    shutil.move(str(src), str(target))
                elif src.is_dir() and not src.is_symlink():
                    shutil.copytree(src, target, symlinks=True)
                else:
                    shutil.copy2(src, target, follow_symlinks=False)
                count += 1
            except Exception as e:  # noqa: BLE001 - report every failure to the user
                errors.append(f"{src.name}: {e}")
        verb = "Moved" if op == "move" else "Copied"
        self.done.emit(f"{verb} {count} item{'s' if count != 1 else ''}", errors)


# ---------------------------------------------------------------- layouts & thumbnails

# The eight Windows 10 File Explorer layouts, biggest to smallest (⌘+scroll steps through them).
VIEW_MODES = [
    ("xl", "Extra large icons"), ("large", "Large icons"), ("medium", "Medium icons"),
    ("small", "Small icons"), ("list", "List"), ("details", "Details"), ("tiles", "Tiles"),
    ("content", "Content"),
]
VIEW_LABELS = dict(VIEW_MODES)

# How the icon view is set up for each non-Details layout.
#   icon: icon size; grid: cell size (None = row sized by the delegate);
#   icon_mode: text under icon; flow: "lr" rows or "tb" columns; wrap: wrap into rows/columns.
ICON_LAYOUTS = {
    "xl":      dict(icon=256, grid=(276, 304), icon_mode=True, flow="lr", wrap=True),
    "large":   dict(icon=96, grid=(124, 150), icon_mode=True, flow="lr", wrap=True),
    "medium":  dict(icon=48, grid=(96, 96), icon_mode=True, flow="lr", wrap=True),
    "small":   dict(icon=16, grid=(210, 24), icon_mode=False, flow="lr", wrap=True),
    "list":    dict(icon=16, grid=(240, 24), icon_mode=False, flow="tb", wrap=True),
    "tiles":   dict(icon=48, grid=(270, 70), icon_mode=False, flow="lr", wrap=True,
                    delegate="tiles"),
    "content": dict(icon=32, grid=None, icon_mode=False, flow="tb", wrap=False,
                    delegate="content"),
}

THUMB_MAX_BYTES = 80 * 1024 * 1024
THUMB_CACHE_ENTRIES = 600


class ThumbnailCache(QObject):
    """Loads image thumbnails on worker threads; shared by every tab."""

    ready = Signal(str)
    _loaded = Signal(str, int, float, QImage)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache: OrderedDict[tuple[str, int], tuple[float, QPixmap]] = OrderedDict()
        self._pending: set[tuple[str, int]] = set()
        self._pool = ThreadPoolExecutor(max_workers=3)
        self._loaded.connect(self._store)
        self.exts = {bytes(f).decode().lower() for f in QImageReader.supportedImageFormats()}

    def get(self, path: str, size: int) -> QPixmap | None:
        """Return a cached thumbnail, or None (and start loading it) if not ready yet."""
        if os.path.splitext(path)[1][1:].lower() not in self.exts:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (path, size)
        hit = self._cache.get(key)
        if hit and hit[0] == st.st_mtime:
            self._cache.move_to_end(key)
            return hit[1]
        if st.st_size <= THUMB_MAX_BYTES and key not in self._pending:
            self._pending.add(key)
            self._pool.submit(self._load, path, size, st.st_mtime)
        return None

    def _load(self, path: str, size: int, mtime: float) -> None:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        full = reader.size()
        if full.isValid() and (full.width() > size or full.height() > size):
            reader.setScaledSize(full.scaled(size, size, Qt.KeepAspectRatio))
        self._loaded.emit(path, size, mtime, reader.read())

    def _store(self, path: str, size: int, mtime: float, image: QImage) -> None:
        key = (path, size)
        self._pending.discard(key)
        pix = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
        self._cache[key] = (mtime, pix)
        while len(self._cache) > THUMB_CACHE_ENTRIES:
            self._cache.popitem(last=False)
        if not pix.isNull():
            self.ready.emit(path)


_thumbnails: ThumbnailCache | None = None


def thumbnail_cache() -> ThumbnailCache:
    global _thumbnails
    if _thumbnails is None:
        _thumbnails = ThumbnailCache(QApplication.instance())
    return _thumbnails


# ---------------------------------------------------------------- browser tab

class FolderFilterProxy(QSortFilterProxyModel):
    """Filters the current folder by name and sorts Finder-style (natural order)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""
        self._root_path = ""
        self._collator = QCollator()
        self._collator.setNumericMode(True)
        self._collator.setCaseSensitivity(Qt.CaseInsensitive)
        self.setDynamicSortFilter(True)
        self.thumb_size = 0  # >0: show image thumbnails at this pixel size
        thumbnail_cache().ready.connect(self._on_thumbnail_ready)

    def data(self, index, role=Qt.DisplayRole):
        if role == Qt.DecorationRole and self.thumb_size and index.column() == 0:
            path = self.sourceModel().filePath(self.mapToSource(index))
            pix = thumbnail_cache().get(path, self.thumb_size)
            if pix is not None and not pix.isNull():
                return QIcon(pix)
        return super().data(index, role)

    def _on_thumbnail_ready(self, path: str) -> None:
        if self.thumb_size and os.path.dirname(path) == self._root_path:
            idx = self.mapFromSource(self.sourceModel().index(path))
            if idx.isValid():
                self.dataChanged.emit(idx, idx, [Qt.DecorationRole])

    def set_root_path(self, path: str) -> None:
        self._refilter(lambda: setattr(self, "_root_path", path))

    def set_text(self, text: str) -> None:
        self._refilter(lambda: setattr(self, "text", text))

    def _refilter(self, change) -> None:
        if hasattr(self, "beginFilterChange"):  # Qt 6.9+
            self.beginFilterChange()
            change()
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            change()
            self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if not self.text:
            return True
        src = self.sourceModel()
        if src.filePath(parent) != self._root_path:
            return True  # only filter direct children of the folder being shown
        return self.text.lower() in src.fileName(src.index(row, 0, parent)).lower()

    def lessThan(self, left, right):
        src = self.sourceModel()
        col = left.column()
        if col == 1:
            a = -1 if src.isDir(left) else src.size(left)
            b = -1 if src.isDir(right) else src.size(right)
            if a != b:
                return a < b
        elif col == 2:
            a, b = src.type(left).lower(), src.type(right).lower()
            if a != b:
                return a < b
        elif col == 3:
            a, b = src.lastModified(left), src.lastModified(right)
            if a != b:
                return a < b
        return self._collator.compare(src.fileName(left), src.fileName(right)) < 0


class ViewBehavior:
    """Keyboard, mouse and drag-and-drop behaviour shared by the details and icon views.

    Concrete views must define the signals used here.
    """

    _zoom_accum = 0.0

    def event(self, e):
        # Tab switches between panes (like a dual-pane commander) instead of moving focus.
        if e.type() == e.Type.KeyPress and e.key() == Qt.Key_Tab and not e.modifiers():
            self.switch_pane_requested.emit()
            return True
        if e.type() == e.Type.NativeGesture and e.gestureType() == Qt.ZoomNativeGesture:
            self._zoom_by(e.value() * 4)  # trackpad pinch
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space and not e.modifiers():
            self.quicklook_requested.emit()
            return
        super().keyPressEvent(e)

    def wheelEvent(self, e):
        # ⌘ + scroll wheel changes the layout, like Ctrl + wheel in Windows Explorer.
        if e.modifiers() & Qt.ControlModifier:
            self._zoom_by(e.angleDelta().y() / 120)
            e.accept()
            return
        super().wheelEvent(e)

    def _zoom_by(self, amount: float) -> None:
        self._zoom_accum += amount
        while abs(self._zoom_accum) >= 1:
            step = 1 if self._zoom_accum > 0 else -1
            self._zoom_accum -= step
            self.zoom_requested.emit(step)

    # Double-click is detected from the two presses ourselves instead of relying on Qt's
    # doubleClicked signal, which is silently dropped when the item under the cursor changes
    # between the clicks (items re-sorting while a folder loads, a thumbnail arriving, …).
    # The item opened is the one under the *first* click, tracked even if it moves.
    _first_press = None      # (timestamp ms, position, QPersistentModelIndex)
    _opened_by_press = False  # this click already opened the item; ignore Qt's double-click

    def mousePressEvent(self, e):
        self._opened_by_press = False
        if e.button() == Qt.LeftButton and not e.modifiers() & (Qt.ShiftModifier |
                                                                  Qt.ControlModifier):
            pos = e.position().toPoint()
            first = self._first_press
            if first and self._is_second_click(e, first):
                self._first_press = None
                target = QModelIndex(first[2]) if first[2].isValid() else self.indexAt(pos)
                super().mousePressEvent(e)
                if target.isValid():
                    self._opened_by_press = True
                    self.item_double_clicked.emit(target)
                return
            idx = self.indexAt(pos)
            self._first_press = (e.timestamp(), pos, QPersistentModelIndex(idx)) \
                if idx.isValid() else None
        else:
            self._first_press = None
        super().mousePressEvent(e)

    @staticmethod
    def _is_second_click(e, first) -> bool:
        t0, p0, _ = first
        moved = (e.position().toPoint() - p0).manhattanLength()
        return (0 <= e.timestamp() - t0 <= QApplication.doubleClickInterval()
                and moved <= max(6, QApplication.startDragDistance()))

    def mouseDoubleClickEvent(self, e):
        if e.button() != Qt.LeftButton:
            return super().mouseDoubleClickEvent(e)
        e.accept()
        if self._opened_by_press:
            self._opened_by_press = False
            return  # already opened from the second press
        idx = self.indexAt(e.position().toPoint())
        if idx.isValid():  # Qt sent a double-click without a second press event
            self._first_press = None
            self.item_double_clicked.emit(idx)

    def dragEnterEvent(self, e):
        super().dragEnterEvent(e)
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        super().dragMoveEvent(e)
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        if not paths:
            return super().dropEvent(e)
        force_copy = bool(e.modifiers() & Qt.AltModifier)
        self.files_dropped.emit(paths, self.indexAt(e.position().toPoint()), force_copy)
        # Report a copy to the drag source so Qt never tries to remove the source rows;
        # the actual move/copy is done by FileOps.
        e.setDropAction(Qt.CopyAction)
        e.accept()

    def setup_common(self) -> None:
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditKeyPressed)  # Return renames, like Finder
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setContextMenuPolicy(Qt.CustomContextMenu)


class FileView(ViewBehavior, QTreeView):
    """The Details layout: sortable columns."""

    quicklook_requested = Signal()
    item_double_clicked = Signal(QModelIndex)
    switch_pane_requested = Signal()
    zoom_requested = Signal(int)
    files_dropped = Signal(list, QModelIndex, bool)  # paths, target index, force copy

    COLUMN_WIDTHS = {1: 90, 2: 140, 3: 150}  # Size, Kind, Date Modified
    MIN_NAME_WIDTH = 200

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit_columns()

    def fit_columns(self) -> None:
        """Give Name the leftover width; hide Date, then Kind, then Size when too narrow."""
        h = self.header()
        width = self.viewport().width()
        visible = [1, 2, 3]
        for col in (3, 2, 1):
            if width - sum(self.COLUMN_WIDTHS[c] for c in visible) >= self.MIN_NAME_WIDTH:
                break
            visible.remove(col)
        for col in (1, 2, 3):
            if h.isSectionHidden(col) != (col not in visible):
                h.setSectionHidden(col, col not in visible)
                if col in visible:
                    h.resizeSection(col, self.COLUMN_WIDTHS[col])
        used = sum(h.sectionSize(c) for c in visible)
        h.resizeSection(0, max(self.MIN_NAME_WIDTH, width - used))


class NameDelegate(QStyledItemDelegate):
    """When renaming, select the name without its extension, like Finder and Windows."""

    def setEditorData(self, editor, index):
        super().setEditorData(editor, index)
        if isinstance(editor, QLineEdit):
            name = editor.text()
            path = index.data(QFileSystemModel.FilePathRole) or ""
            n = len(name) if os.path.isdir(path) else len(Path(name).stem)
            # Qt selects everything after this call, so apply our selection afterwards.
            QTimer.singleShot(0, lambda: editor.setSelection(0, n))


class _DetailDelegate(NameDelegate):
    """Base for Tiles/Content: draws the icon plus several lines of file details."""

    def _paint_base(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        icon, name = QIcon(opt.icon), str(opt.text)  # copies: opt is cleared below
        opt.text, opt.icon = "", QIcon()
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)  # selection background
        selected = bool(opt.state & QStyle.State_Selected)
        main = opt.palette.highlightedText().color() if selected else opt.palette.text().color()
        dim = main if selected else opt.palette.placeholderText().color()
        return opt, icon, name, main, dim

    @staticmethod
    def _column(index, col: int) -> str:
        text = index.siblingAtColumn(col).data() or ""
        return "" if text == "--" else text

    @staticmethod
    def _draw_lines(painter, opt, rect: QRect, lines, align=Qt.AlignLeft) -> None:
        fm = opt.fontMetrics
        lh = fm.height() + 1
        y = rect.top() + (rect.height() - lh * len(lines)) // 2
        for i, (text, color) in enumerate(lines):
            painter.setPen(color)
            painter.drawText(QRect(rect.left(), y + i * lh, rect.width(), lh),
                             align | Qt.AlignVCenter,
                             fm.elidedText(text, Qt.ElideRight, rect.width()))


class TileDelegate(_DetailDelegate):
    """Tiles: medium icon with name, kind and size beside it."""

    def paint(self, painter, option, index):
        opt, icon, name, main, dim = self._paint_base(painter, option, index)
        r = opt.rect.adjusted(6, 4, -6, -4)
        isz = opt.decorationSize
        icon.paint(painter, QRect(r.left(), r.top() + (r.height() - isz.height()) // 2,
                                  isz.width(), isz.height()))
        lines = [(name, main), (self._column(index, 2), dim)]
        if size := self._column(index, 1):
            lines.append((size, dim))
        painter.save()
        self._draw_lines(painter, opt, r.adjusted(isz.width() + 10, 0, 0, 0), lines)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(270, 70)


class ContentDelegate(_DetailDelegate):
    """Content: one wide row per item — name and kind left, date and size right."""

    ROW_HEIGHT = 54

    def paint(self, painter, option, index):
        opt, icon, name, main, dim = self._paint_base(painter, option, index)
        r = opt.rect.adjusted(10, 4, -12, -4)
        isz = opt.decorationSize
        icon.paint(painter, QRect(r.left(), r.top() + (r.height() - isz.height()) // 2,
                                  isz.width(), isz.height()))
        right_w = min(260, r.width() // 2)
        left = r.adjusted(isz.width() + 12, 0, -right_w - 12, 0)
        right = QRect(r.right() - right_w, r.top(), right_w, r.height())
        painter.save()
        self._draw_lines(painter, opt, left, [(name, main), (self._column(index, 2), dim)])
        details = [(f"Date modified: {self._column(index, 3)}", dim)]
        if size := self._column(index, 1):
            details.append((f"Size: {size}", dim))
        self._draw_lines(painter, opt, right, details)
        painter.setPen(opt.palette.mid().color())
        painter.drawLine(opt.rect.left() + 8, opt.rect.bottom(), opt.rect.right() - 8,
                         opt.rect.bottom())
        painter.restore()

    def sizeHint(self, option, index):
        w = option.widget.viewport().width() if option.widget else 400
        return QSize(max(300, w - 2), self.ROW_HEIGHT)


class IconView(ViewBehavior, QListView):
    """Every layout except Details: icons, small icons, list, tiles, content."""

    quicklook_requested = Signal()
    item_double_clicked = Signal(QModelIndex)
    switch_pane_requested = Signal()
    zoom_requested = Signal(int)
    files_dropped = Signal(list, QModelIndex, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._default_delegate = NameDelegate(self)
        self._delegates = {"tiles": TileDelegate(self), "content": ContentDelegate(self)}

    def configure(self, mode: str) -> None:
        cfg = ICON_LAYOUTS[mode]
        self.setItemDelegate(self._delegates.get(cfg.get("delegate"), self._default_delegate))
        self.setViewMode(QListView.IconMode if cfg["icon_mode"] else QListView.ListMode)
        self.setMovement(QListView.Static)  # setViewMode resets this; icons stay in the grid
        self.setFlow(QListView.LeftToRight if cfg["flow"] == "lr" else QListView.TopToBottom)
        self.setWrapping(cfg["wrap"])
        self.setResizeMode(QListView.Adjust)
        # Uniform sizes would size every item like the first one (cutting off long names)
        self.setUniformItemSizes(mode == "content")
        self.setSelectionRectVisible(True)
        self.setIconSize(QSize(cfg["icon"], cfg["icon"]))
        self.setGridSize(QSize(*cfg["grid"]) if cfg["grid"] else QSize())
        self.setWordWrap(cfg["icon_mode"])
        self.setTextElideMode(Qt.ElideMiddle if cfg["icon_mode"] else Qt.ElideRight)
        self.setSpacing(0)
        self.doItemsLayout()


class PathEdit(QLineEdit):
    escaped = Signal()

    def keyPressEvent(self, e):
        popup = self.completer().popup() if self.completer() else None
        if e.key() == Qt.Key_Escape and not (popup and popup.isVisible()):
            self.escaped.emit()
            return
        super().keyPressEvent(e)


class BrowserTab(QWidget):
    location_changed = Signal(object)
    selection_changed = Signal(object)
    contents_changed = Signal(object)
    new_tab_requested = Signal(str)
    open_failed = Signal(str)
    message = Signal(str)
    drop_requested = Signal(str, list, str)  # op, sources, dest dir
    context_menu_requested = Signal(object)    # QPoint in the current view's viewport
    quicklook_requested = Signal()
    switch_pane_requested = Signal()
    zoom_requested = Signal(int)

    def __init__(self, path: str, show_hidden: bool, parent=None):
        super().__init__(parent)
        self.path = ""
        self.back: list[str] = []
        self.forward: list[str] = []

        self.model = QFileSystemModel(self)
        self.model.setReadOnly(False)  # enables inline rename
        self.set_show_hidden(show_hidden)
        self.proxy = FolderFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        # Details layout
        tree = self.tree = FileView(self)
        tree.setModel(self.proxy)
        tree.setItemDelegate(NameDelegate(tree))
        tree.setup_common()
        tree.setSortingEnabled(True)
        tree.sortByColumn(0, Qt.AscendingOrder)
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(True)
        tree.setExpandsOnDoubleClick(False)
        header = tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.Interactive)
        for col, width in FileView.COLUMN_WIDTHS.items():
            header.resizeSection(col, width)

        # All other layouts; shares the model *and* the selection with the details view
        self.icons = IconView(self)
        self.icons.setModel(self.proxy)
        self.icons.setSelectionModel(tree.selectionModel())
        self.icons.setup_common()

        for view in (tree, self.icons):
            view.item_double_clicked.connect(self.open_index)
            view.files_dropped.connect(self._on_drop)
            view.quicklook_requested.connect(self.quicklook_requested)
            view.switch_pane_requested.connect(self.switch_pane_requested)
            view.zoom_requested.connect(self.zoom_requested)
            view.customContextMenuRequested.connect(self.context_menu_requested.emit)
        tree.selectionModel().selectionChanged.connect(
            lambda *_: self.selection_changed.emit(self))

        self.stack = QStackedWidget(self)
        self.stack.addWidget(tree)
        self.stack.addWidget(self.icons)
        self.view: QAbstractItemView = tree  # whichever view is showing
        self.mode = "details"
        self.model.directoryLoaded.connect(lambda *_: self.contents_changed.emit(self))
        self.proxy.rowsInserted.connect(lambda *_: self.contents_changed.emit(self))
        self.proxy.rowsRemoved.connect(lambda *_: self.contents_changed.emit(self))

        def tool(icon, tip, slot):
            b = QToolButton(self)
            b.setIcon(self.style().standardIcon(icon))
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(lambda: slot())
            return b

        self.btn_back = tool(QStyle.SP_ArrowBack, "Back (⌘[)", self.go_back)
        self.btn_fwd = tool(QStyle.SP_ArrowForward, "Forward (⌘])", self.go_forward)
        self.btn_up = tool(QStyle.SP_ArrowUp, "Enclosing Folder (⌘↑)", self.go_up)
        self.path_edit = PathEdit(self)
        self.path_edit.setPlaceholderText("Type or paste a path, then press Return")
        self.path_edit.setMinimumWidth(140)
        self.path_edit.setCompleter(path_completer(self))
        self.path_edit.returnPressed.connect(lambda: self.go_to(self.path_edit.text()))
        self.path_edit.escaped.connect(self._reset_path_edit)
        self.btn_copy_path = QToolButton(self, text="Copy Path")
        self.btn_copy_path.setToolTip("Copy this folder's path to the clipboard")
        self.btn_copy_path.clicked.connect(lambda: self.copy_folder_path())

        nav = QHBoxLayout()
        nav.setContentsMargins(4, 4, 4, 2)
        nav.setSpacing(2)
        for w in (self.btn_back, self.btn_fwd, self.btn_up):
            nav.addWidget(w)
        nav.addSpacing(4)
        nav.addWidget(self.path_edit, 1)
        nav.addWidget(self.btn_copy_path)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(nav)
        layout.addWidget(self.stack)

        if not self.navigate(path, record=False):
            self.navigate(HOME, record=False)

    # -- navigation

    def navigate(self, path: str, record: bool = True) -> bool:
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path) or not os.access(path, os.R_OK | os.X_OK):
            return False
        if record and self.path and path != self.path:
            self.back.append(self.path)
            self.forward.clear()
        self.path = path
        self.proxy.set_text("")
        src_root = self.model.setRootPath(path)
        self.proxy.set_root_path(self.model.filePath(src_root))
        root = self.proxy.mapFromSource(src_root)
        for view in (self.tree, self.icons):
            view.setRootIndex(root)
            view.scrollToTop()
        self._reset_path_edit()
        self.btn_back.setEnabled(bool(self.back))
        self.btn_fwd.setEnabled(bool(self.forward))
        self.btn_up.setEnabled(path != "/")
        self.location_changed.emit(self)
        return True

    def set_view_mode(self, mode: str) -> None:
        if mode not in VIEW_LABELS:
            mode = "details"
        had_focus = self.view.hasFocus()
        current = self.view.currentIndex()
        self.mode = mode
        if mode == "details":
            self.view = self.tree
            self.proxy.thumb_size = 0
        else:
            self.icons.configure(mode)
            self.view = self.icons
            px = ICON_LAYOUTS[mode]["icon"]
            self.proxy.thumb_size = int(px * self.devicePixelRatioF()) if px >= 48 else 0
        self.stack.setCurrentWidget(self.view)
        self.view.viewport().update()
        if current.isValid():
            self.view.scrollTo(current)
        if had_focus:
            self.view.setFocus()

    def sort_by(self, column: int | None = None, order=None) -> None:
        h = self.tree.header()
        column = h.sortIndicatorSection() if column is None else column
        order = h.sortIndicatorOrder() if order is None else order
        self.tree.sortByColumn(column, order)

    def go_to(self, text: str) -> bool:
        """Go to a typed or pasted path; a file path opens its folder and selects the file."""
        path = parse_path(text)
        ok = False
        if path and os.path.isdir(path) and not QFileInfo(path).isBundle():
            ok = self.navigate(path)
            if not ok:
                self.open_failed.emit(path)
                return False
        elif path:
            ok = self.navigate(os.path.dirname(path))
            if ok:
                self.select_path(path)
        if ok:
            self.view.setFocus()
        else:
            self.message.emit(f"Can't find “{text.strip()}”")
        return ok

    def _reset_path_edit(self) -> None:
        self.path_edit.setText(self.path)
        if self.path_edit.hasFocus():
            self.view.setFocus()

    def copy_folder_path(self) -> None:
        QGuiApplication.clipboard().setText(self.path)
        self.message.emit(f"Copied path: {self.path}")

    def go_back(self) -> None:
        if self.back:
            prev = self.path
            self.forward.append(prev)
            if self.navigate(self.back.pop(), record=False):
                self.select_path(prev)
            else:
                self.forward.pop()

    def go_forward(self) -> None:
        if self.forward:
            self.back.append(self.path)
            if not self.navigate(self.forward.pop(), record=False):
                self.back.pop()

    def go_up(self) -> None:
        parent = os.path.dirname(self.path)
        if parent != self.path:
            prev = self.path
            if self.navigate(parent):
                self.select_path(prev)

    def select_path(self, path: str) -> None:
        idx = self.proxy.mapFromSource(self.model.index(path))
        if idx.isValid():
            self.view.setCurrentIndex(idx)
            self.view.scrollTo(idx)

    def set_show_hidden(self, show: bool) -> None:
        f = QDir.AllEntries | QDir.NoDotAndDotDot | QDir.System
        if show:
            f |= QDir.Hidden
        self.model.setFilter(f)

    # -- selection & opening

    def selected_paths(self) -> list[str]:
        rows = self.view.selectionModel().selectedRows(0)
        return [self.model.filePath(self.proxy.mapToSource(i)) for i in rows]

    def selected_size(self) -> int:
        total = 0
        for i in self.view.selectionModel().selectedRows(0):
            src = self.proxy.mapToSource(i)
            if not self.model.isDir(src):
                total += self.model.size(src)
        return total

    def item_count(self) -> int:
        return self.proxy.rowCount(self.view.rootIndex())

    def open_index(self, index: QModelIndex) -> None:
        info = self.model.fileInfo(self.proxy.mapToSource(index.siblingAtColumn(0)))
        if info.isDir() and not info.isBundle():
            if not self.navigate(info.absoluteFilePath()):
                self.open_failed.emit(info.absoluteFilePath())
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(info.absoluteFilePath()))

    def open_selection(self) -> None:
        rows = self.view.selectionModel().selectedRows(0)
        if len(rows) == 1:
            self.open_index(rows[0])
            return
        for path in self.selected_paths():
            info = QFileInfo(path)
            if info.isDir() and not info.isBundle():
                self.new_tab_requested.emit(path)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_drop(self, paths: list, index: QModelIndex, force_copy: bool) -> None:
        dest = self.path
        if index.isValid():
            info = self.model.fileInfo(self.proxy.mapToSource(index.siblingAtColumn(0)))
            if info.isDir() and not info.isBundle():
                dest = info.absoluteFilePath()
        # Finder rules: same volume moves, another volume copies, Option forces a copy.
        try:
            same_volume = os.stat(paths[0]).st_dev == os.stat(dest).st_dev
        except OSError:
            same_volume = False
        op = "copy" if force_copy or not same_volume else "move"
        self.drop_requested.emit(op, paths, dest)


class Pane(QFrame):
    """One side of the window: a tab widget holding BrowserTabs."""

    def __init__(self, window: MainWindow):
        super().__init__()
        self.setObjectName("pane")
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        plus = QToolButton(text="+")
        plus.setToolTip("New Tab (⌘T)")
        plus.setAutoRaise(True)
        plus.clicked.connect(lambda: window.new_tab(self))
        self.tabs.setCornerWidget(plus, Qt.TopRightCorner)
        self.tabs.tabCloseRequested.connect(lambda i: window.close_tab(self, i))
        self.tabs.currentChanged.connect(lambda _: window.on_tab_switched(self))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.addWidget(self.tabs)

    def current(self) -> BrowserTab:
        return self.tabs.currentWidget()

    def all_tabs(self) -> list[BrowserTab]:
        return [self.tabs.widget(i) for i in range(self.tabs.count())]


# ---------------------------------------------------------------- sidebar

class Sidebar(QTreeWidget):
    navigate_requested = Signal(str)
    favorites_changed = Signal()

    def __init__(self, favorites: list[str]):
        super().__init__()
        self.setHeaderHidden(True)
        self.setIndentation(10)
        self.setRootIsDecorated(False)
        self.setIconSize(QSize(16, 16))
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.itemClicked.connect(self._activate)
        self.itemActivated.connect(self._activate)
        self.icons = QFileIconProvider()

        self.fav_root = self._section("Favorites")
        self.loc_root = self._section("Locations")
        self.set_favorites(favorites)
        self.refresh_locations()
        self.watcher = QFileSystemWatcher(["/Volumes"], self)
        self.watcher.directoryChanged.connect(lambda _: self.refresh_locations())

    def _section(self, title: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self, [title])
        item.setFlags(Qt.ItemIsEnabled)
        font = QFont(item.font(0))
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 0.85)
        item.setFont(0, font)
        item.setForeground(0, self.palette().placeholderText())
        item.setExpanded(True)
        return item

    def _add(self, parent: QTreeWidgetItem, path: str, name: str | None = None) -> None:
        item = QTreeWidgetItem(parent, [name or display_name(path)])
        item.setData(0, Qt.UserRole, path)
        item.setIcon(0, self.icons.icon(QFileInfo(path)))
        item.setToolTip(0, path)

    def set_favorites(self, paths: list[str]) -> None:
        self.fav_root.takeChildren()
        for p in paths:
            if os.path.isdir(p):
                self._add(self.fav_root, p)

    def favorites(self) -> list[str]:
        return [self.fav_root.child(i).data(0, Qt.UserRole)
                for i in range(self.fav_root.childCount())]

    def add_favorite(self, path: str) -> None:
        if path not in self.favorites():
            self._add(self.fav_root, path)
            self.favorites_changed.emit()

    def refresh_locations(self) -> None:
        self.loc_root.takeChildren()
        self._add(self.loc_root, "/")
        icloud = os.path.join(HOME, "Library/Mobile Documents/com~apple~CloudDocs")
        if os.path.isdir(icloud):
            self._add(self.loc_root, icloud, "iCloud Drive")
        try:
            vols = sorted(os.scandir("/Volumes"), key=lambda d: d.name.lower())
        except OSError:
            vols = []
        for d in vols:
            if not d.is_symlink() and d.is_dir():  # the boot volume is a symlink to /
                self._add(self.loc_root, d.path)

    def highlight(self, path: str) -> None:
        self.blockSignals(True)
        self.clearSelection()
        for root in (self.fav_root, self.loc_root):
            for i in range(root.childCount()):
                item = root.child(i)
                if item.data(0, Qt.UserRole) == path:
                    self.setCurrentItem(item)
                    self.blockSignals(False)
                    return
        self.setCurrentItem(None)
        self.blockSignals(False)

    def _activate(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, Qt.UserRole)
        if path:
            self.navigate_requested.emit(path)

    def _context_menu(self, pos) -> None:
        item = self.itemAt(pos)
        if not item or item.parent() is not self.fav_root:
            return
        menu = QMenu(self)
        menu.addAction("Remove from Sidebar", lambda: self._remove(item))
        menu.exec(self.viewport().mapToGlobal(pos))

    def _remove(self, item: QTreeWidgetItem) -> None:
        self.fav_root.removeChild(item)
        self.favorites_changed.emit()


# ---------------------------------------------------------------- preview

TEXT_SNIFF_BYTES = 128 * 1024


class PreviewPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumWidth(200)
        self.image = QLabel(alignment=Qt.AlignCenter)
        self.image.setMinimumHeight(140)
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.info = QLabel(wordWrap=True)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self.image)
        layout.addWidget(self.text, 1)
        layout.addWidget(self.info)
        layout.addStretch(0)

        self.icons = QFileIconProvider()
        self.mime = QMimeDatabase()
        self.image_exts = {bytes(f).decode().lower() for f in QImageReader.supportedImageFormats()}
        self.tmp = tempfile.mkdtemp(prefix="unfinder-ql-")
        self._path: str | None = None
        self._pix: QPixmap | None = None
        self._scale = False
        self._gen = 0
        self._proc: QProcess | None = None
        self.show_path(None)

    def show_path(self, path: str | None) -> None:
        self._gen += 1
        self._stop_thumbnail()
        self._path = path
        self._pix = None
        self.image.clear()
        self.text.hide()
        self.text.clear()
        if not path:
            self.image.hide()
            self.info.setText("<p style='color:gray'>Select one item to preview it.</p>")
            return
        self.image.show()
        info = QFileInfo(path)
        self._set_info(info)
        ext = info.suffix().lower()
        if info.isDir() and not info.isBundle():
            self._set_icon(info)
        elif ext in self.image_exts and QPixmap(path).isNull() is False:
            self._set_pixmap(QPixmap(path), scale=True)
        elif info.isFile() and self._try_text(path):
            self.image.hide()
        else:
            self._set_icon(info)
            self._request_thumbnail(path)

    def _set_info(self, info: QFileInfo) -> None:
        rows = [("Kind", "Folder" if info.isDir() and not info.isBundle()
                 else self.mime.mimeTypeForFile(info).comment())]
        if info.isDir() and not info.isBundle():
            try:
                rows.append(("Items", str(len(os.listdir(info.absoluteFilePath())))))
            except OSError:
                pass
        else:
            rows.append(("Size", f"{human_size(info.size())} ({info.size():,} bytes)"))
        fmt = "MMM d, yyyy 'at' h:mm AP"
        if info.birthTime().isValid():
            rows.append(("Created", info.birthTime().toString(fmt)))
        rows.append(("Modified", info.lastModified().toString(fmt)))
        rows.append(("Where", info.absolutePath()))
        body = "".join(
            f"<tr><td style='color:gray;padding-right:8px'>{k}</td><td>{html.escape(v)}</td></tr>"
            for k, v in rows)
        self.info.setText(f"<b>{html.escape(info.fileName() or info.absoluteFilePath())}</b>"
                          f"<table style='margin-top:6px'>{body}</table>")

    def _try_text(self, path: str) -> bool:
        try:
            with open(path, "rb") as f:
                chunk = f.read(TEXT_SNIFF_BYTES)
        except OSError:
            return False
        if not chunk or b"\0" in chunk:
            return False
        text = chunk.decode("utf-8", errors="replace")
        if text.count("�") > 2:
            return False
        if len(chunk) == TEXT_SNIFF_BYTES:
            text += "\n\n… (preview truncated)"
        self.text.setPlainText(text)
        self.text.show()
        return True

    def _set_icon(self, info: QFileInfo) -> None:
        self._set_pixmap(self.icons.icon(info).pixmap(128, 128), scale=False)

    def _set_pixmap(self, pix: QPixmap, scale: bool) -> None:
        self._pix, self._scale = pix, scale
        self._render()

    def _render(self) -> None:
        if self._pix is None:
            return
        if not self._scale:
            self.image.setPixmap(self._pix)
            return
        dpr = self.devicePixelRatioF()
        w = max(100, self.width() - 24)
        h = max(140, int(self.height() * 0.6))
        scaled = self._pix.scaled(int(w * dpr), int(h * dpr), Qt.KeepAspectRatio,
                                  Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        self.image.setPixmap(scaled)

    def _request_thumbnail(self, path: str) -> None:
        """Ask Quick Look (qlmanage) for a thumbnail — covers PDFs, video, docs, HEIC…"""
        gen = self._gen
        proc = QProcess(self)
        proc.finished.connect(lambda *_: self._thumbnail_ready(proc, gen, path))
        proc.start("/usr/bin/qlmanage", ["-t", "-s", "800", "-o", self.tmp, path])
        self._proc = proc

    def _stop_thumbnail(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and shiboken6.isValid(proc):
            proc.finished.disconnect()  # so a killed process can't call back into us
            proc.kill()
            proc.waitForFinished(500)
            proc.deleteLater()

    def _thumbnail_ready(self, proc: QProcess, gen: int, path: str) -> None:
        if proc is self._proc:
            self._proc = None
        if shiboken6.isValid(proc):
            proc.deleteLater()
        out = os.path.join(self.tmp, os.path.basename(path) + ".png")
        if gen == self._gen and os.path.exists(out):
            pix = QPixmap(out)
            if not pix.isNull():
                self._set_pixmap(pix, scale=True)
        if os.path.exists(out):
            os.remove(out)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._render()

    def cleanup(self) -> None:
        self._stop_thumbnail()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- properties

def size_text(n: int) -> str:
    return f"{human_size(n)} ({n:,} bytes)" if n >= 1000 else f"{n:,} bytes"


class SizeCounter(QObject):
    """Adds up the size of files and folders on a background thread."""

    progress = Signal(object, object, int, int, bool)  # bytes, bytes on disk, files, folders, done

    def __init__(self, paths: list[str], parent=None):
        super().__init__(parent)
        self.paths = paths
        self._cancelled = False

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self) -> None:
        self._cancelled = True

    def _run(self) -> None:
        size = disk = files = folders = 0
        last = time.monotonic()
        stack = []
        for p in self.paths:
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                stack.append(p)
            else:
                files += 1
                size += st.st_size
                disk += st.st_blocks * 512
        while stack and not self._cancelled:
            try:
                entries = list(os.scandir(stack.pop()))
            except OSError:
                continue
            for e in entries:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    folders += 1
                    stack.append(e.path)
                else:
                    files += 1
                    size += st.st_size
                    disk += st.st_blocks * 512
            if time.monotonic() - last > 0.2:
                last = time.monotonic()
                self.progress.emit(size, disk, files, folders, False)
        if not self._cancelled:
            self.progress.emit(size, disk, files, folders, True)


class PropertiesDialog(QDialog):
    """Windows-style Properties: General (rename, size, dates, attributes) + Permissions."""

    DATE_FMT = "dddd, d MMMM yyyy, h:mm:ss AP"

    def __init__(self, paths: list[str], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.paths = paths
        self.single = len(paths) == 1
        info = QFileInfo(paths[0])
        self.is_dir = info.isDir() and not info.isSymLink()
        title = display_name(paths[0]) if self.single else f"{len(paths)} items"
        self.setWindowTitle(f"{title} Properties")
        self.setMinimumWidth(520)

        tabs = QTabWidget()
        tabs.addTab(self._general_tab(info), "General")
        if self.single:
            tabs.addTab(self._permissions_tab(), "Permissions")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel |
                                   QDialogButtonBox.Apply)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self.apply)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

        self.counter = SizeCounter(paths, self)
        self.counter.progress.connect(self._on_size)
        self.counter.start()

    # -- General tab

    @staticmethod
    def _value(text: str = "") -> QLabel:
        label = QLabel(text)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _line() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _general_tab(self, info: QFileInfo) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setLabelAlignment(Qt.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)  # macOS default won't

        icon = QLabel()
        icon.setPixmap(QFileIconProvider().icon(info).pixmap(48, 48))
        self.name_edit = QLineEdit(info.fileName() or display_name(info.absoluteFilePath()))
        self.name_edit.setEnabled(self.single and info.fileName() != "")
        if not self.single:
            n_dirs = sum(os.path.isdir(p) for p in self.paths)
            self.name_edit.setText(f"{len(self.paths) - n_dirs} files, {n_dirs} folders")
        form.addRow(icon, self.name_edit)
        form.addRow(self._line())

        if self.single:
            kind = "Folder" if self.is_dir else (
                QMimeDatabase().mimeTypeForFile(info).comment() or "File")
            if info.isBundle():
                kind = "Application" if info.suffix() == "app" else "Package"
            form.addRow("Type:", self._value(kind))
            if info.isSymLink():
                form.addRow("Link target:", self._value(info.symLinkTarget()))
        else:
            form.addRow("Type:", self._value("Multiple types"))
        parents = {os.path.dirname(p) for p in self.paths}
        form.addRow("Location:", self._value(parents.pop() if len(parents) == 1
                                             else "Various folders"))
        self.size_label = self._value("Calculating…")
        self.disk_label = self._value("Calculating…")
        form.addRow("Size:", self.size_label)
        form.addRow("Size on disk:", self.disk_label)
        self.contains_label = None
        if self.is_dir or not self.single:
            self.contains_label = self._value("Calculating…")
            form.addRow("Contains:", self.contains_label)

        if self.single:
            form.addRow(self._line())
            for label, dt in (("Created:", info.birthTime()), ("Modified:", info.lastModified()),
                              ("Accessed:", info.lastRead())):
                if dt.isValid():
                    form.addRow(label, self._value(dt.toString(self.DATE_FMT)))
            form.addRow(self._line())
            st = os.lstat(self.paths[0])
            self.readonly = QCheckBox("Read-only")
            self.readonly.setChecked(not st.st_mode & stat.S_IWUSR)
            self.readonly.setToolTip("Nobody can change this item (removes write permission)")
            self.hidden = QCheckBox("Hidden")
            dotfile = info.fileName().startswith(".")
            self.hidden.setChecked(dotfile or bool(st.st_flags & stat.UF_HIDDEN))
            self.hidden.setEnabled(not dotfile)
            if dotfile:
                self.hidden.setToolTip("Names starting with a dot are always hidden")
            row = QHBoxLayout()
            row.addWidget(self.readonly)
            row.addWidget(self.hidden)
            row.addStretch()
            form.addRow("Attributes:", row)
        return w

    def _on_size(self, size: int, disk: int, files: int, folders: int, done: bool) -> None:
        suffix = "" if done else "  (counting…)"
        self.size_label.setText(size_text(size) + suffix)
        self.disk_label.setText(size_text(disk) + suffix)
        if self.contains_label is not None:
            self.contains_label.setText(f"{files:,} files, {folders:,} folders{suffix}")

    # -- Permissions tab

    def _permissions_tab(self) -> QWidget:
        import grp
        import pwd
        st = os.lstat(self.paths[0])
        w = QWidget()
        form = QFormLayout(w)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        try:
            owner = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            owner = str(st.st_uid)
        try:
            group = grp.getgrgid(st.st_gid).gr_name
        except KeyError:
            group = str(st.st_gid)
        form.addRow("Owner:", self._value(owner))
        form.addRow("Group:", self._value(group))
        grid = QGridLayout()
        for c, title in enumerate(("Read", "Write", "Execute" if not self.is_dir else "Open"),
                                  start=1):
            grid.addWidget(QLabel(f"<b>{title}</b>"), 0, c, Qt.AlignCenter)
        self.perm_boxes: dict[int, QCheckBox] = {}
        bits = [("Owner", (stat.S_IRUSR, stat.S_IWUSR, stat.S_IXUSR)),
                ("Group", (stat.S_IRGRP, stat.S_IWGRP, stat.S_IXGRP)),
                ("Everyone", (stat.S_IROTH, stat.S_IWOTH, stat.S_IXOTH))]
        for r, (who, flags) in enumerate(bits, start=1):
            grid.addWidget(QLabel(who), r, 0)
            for c, flag in enumerate(flags, start=1):
                box = QCheckBox()
                box.setChecked(bool(st.st_mode & flag))
                box.toggled.connect(self._sync_mode_label)
                grid.addWidget(box, r, c, Qt.AlignCenter)
                self.perm_boxes[flag] = box
        form.addRow("Access:", grid)
        self.mode_label = self._value()
        form.addRow("Mode:", self.mode_label)
        self._sync_mode_label()
        return w

    def _perm_bits(self) -> int:
        return sum(flag for flag, box in self.perm_boxes.items() if box.isChecked())

    def _sync_mode_label(self) -> None:
        self.mode_label.setText(f"{stat.filemode(self._perm_bits())[1:]}  ({self._perm_bits():03o})")

    # -- applying changes

    def apply(self) -> bool:
        if not self.single:
            return True
        path = self.paths[0]
        try:
            st = os.lstat(path)
            mode = (st.st_mode & ~0o777) | self._perm_bits()
            # Read-only wins over the Write boxes when it was just toggled
            if self.readonly.isChecked():
                mode &= ~0o222
            elif not mode & stat.S_IWUSR:
                mode |= stat.S_IWUSR
            if stat.S_IMODE(mode) != stat.S_IMODE(st.st_mode):
                os.chmod(path, stat.S_IMODE(mode))
            if self.hidden.isEnabled():
                flags = st.st_flags | stat.UF_HIDDEN if self.hidden.isChecked() \
                    else st.st_flags & ~stat.UF_HIDDEN
                if flags != st.st_flags:
                    os.chflags(path, flags, follow_symlinks=False)
            new_name = self.name_edit.text().strip()
            if self.name_edit.isEnabled() and new_name != os.path.basename(path):
                new_path = rename_item(self, path, new_name)
                if new_path is None:
                    return False
                self.paths = [new_path]
                self.setWindowTitle(f"{display_name(new_path)} Properties")
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't apply the changes:\n\n{e}")
            return False
        # Refresh check boxes from disk so they show what actually happened
        st = os.lstat(self.paths[0])
        self.readonly.setChecked(not st.st_mode & stat.S_IWUSR)
        for flag, box in self.perm_boxes.items():
            box.setChecked(bool(st.st_mode & flag))
        return True

    def _ok(self) -> None:
        if self.apply():
            self.accept()

    def done(self, result):
        self.counter.cancel()
        super().done(result)


def rename_item(parent: QWidget, path: str, new_name: str) -> str | None:
    """Rename path to new_name in the same folder. Returns the new path, or None."""
    new_name = new_name.strip()
    if not new_name or "/" in new_name or new_name in (".", ".."):
        QMessageBox.warning(parent, APP_NAME, f"“{new_name}” isn't a valid name.")
        return None
    new_path = os.path.join(os.path.dirname(path), new_name)
    if new_path == path:
        return path
    # Allow changing only the capitalisation (the macOS disk is case-insensitive)
    if os.path.lexists(new_path) and os.path.normcase(new_name) != os.path.normcase(
            os.path.basename(path)) and not os.path.samefile(path, new_path):
        QMessageBox.warning(parent, APP_NAME, f"There's already an item named “{new_name}”.")
        return None
    old_ext, new_ext = Path(path).suffix, Path(new_name).suffix
    if not os.path.isdir(path) and old_ext.lower() != new_ext.lower():
        answer = QMessageBox.question(
            parent, APP_NAME, f"If you change a file name extension from “{old_ext or 'none'}” "
                              f"to “{new_ext or 'none'}”, the file might become unusable.\n\n"
                              "Are you sure you want to change it?")
        if answer != QMessageBox.Yes:
            return None
    try:
        os.rename(path, new_path)
    except OSError as e:
        QMessageBox.warning(parent, APP_NAME, f"Couldn't rename “{os.path.basename(path)}”:\n\n{e}")
        return None
    return new_path


# ---------------------------------------------------------------- custom commands

DEFAULT_COMMANDS = [{"name": "Open with VS Code", "command": "code .", "terminal": False}]

# Apps started from the Dock/Finder get a minimal PATH, so tools like `code`, `git` or
# Homebrew programs wouldn't be found. Commands run in a login shell with these added.
EXTRA_PATH = [os.path.join(HOME, ".local/bin"), "/opt/homebrew/bin", "/opt/homebrew/sbin",
              "/usr/local/bin",
              "/Applications/Visual Studio Code.app/Contents/Resources/app/bin"]

COMMAND_HELP = (
    "Commands run in the folder you're viewing, using your shell (so <code>cd</code>, "
    "pipes and <code>&amp;&amp;</code> work).<br>Placeholders: "
    "<code>{folder}</code> current folder · <code>{file}</code> first selected item · "
    "<code>{files}</code> all selected items · <code>{name}</code> its name.<br>"
    "With nothing selected, <code>{file}</code>/<code>{files}</code> mean the current folder. "
    "Examples: <code>code .</code> · <code>code {files}</code> · <code>git status</code> "
    "(tick Terminal to see output) · <code>zip -r archive.zip {files}</code>")


def expand_command(template: str, folder: str, paths: list[str]) -> str:
    """Fill in the placeholders, shell-quoting every path."""
    q = shlex.quote
    sel = paths or [folder]
    return (template.replace("{folder}", q(folder))
            .replace("{files}", " ".join(q(p) for p in sel))
            .replace("{file}", q(sel[0]))
            .replace("{name}", q(os.path.basename(sel[0].rstrip("/")))))


def applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class CommandsDialog(QDialog):
    """Edit the custom commands shown in the right-click menu."""

    def __init__(self, commands: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom Commands")
        self.resize(720, 420)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name in menu", "Command", "Run in Terminal"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(0, 200)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        for c in commands:
            self._add_row(c)

        add = QPushButton("Add")
        add.clicked.connect(lambda: self._add_row(edit=True))
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove)
        up = QPushButton("Move Up")
        up.clicked.connect(lambda: self._move(-1))
        down = QPushButton("Move Down")
        down.clicked.connect(lambda: self._move(1))
        defaults = QPushButton("Add “Open with VS Code”")
        defaults.clicked.connect(lambda: self._add_row(DEFAULT_COMMANDS[0]))
        side = QVBoxLayout()
        for b in (add, remove, up, down, defaults):
            side.addWidget(b)
        side.addStretch()

        top = QHBoxLayout()
        top.addWidget(self.table, 1)
        top.addLayout(side)
        help_label = QLabel(COMMAND_HELP)
        help_label.setWordWrap(True)
        help_label.setTextFormat(Qt.RichText)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(top, 1)
        layout.addWidget(help_label)
        layout.addWidget(buttons)

    def _add_row(self, command: dict | None = None, edit: bool = False) -> None:
        command = command or {"name": "New command", "command": "", "terminal": False}
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(command["name"]))
        self.table.setItem(r, 1, QTableWidgetItem(command["command"]))
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        check.setCheckState(Qt.Checked if command.get("terminal") else Qt.Unchecked)
        self.table.setItem(r, 2, check)
        if edit:
            self.table.setCurrentCell(r, 0)
            self.table.editItem(self.table.item(r, 0))

    def _remove(self) -> None:
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    def _move(self, step: int) -> None:
        r = self.table.currentRow()
        target = r + step
        if r < 0 or not 0 <= target < self.table.rowCount():
            return
        rows = self.commands(keep_empty=True)
        rows[r], rows[target] = rows[target], rows[r]
        self.table.setRowCount(0)
        for c in rows:
            self._add_row(c)
        self.table.setCurrentCell(target, 0)

    def commands(self, keep_empty: bool = False) -> list[dict]:
        out = []
        for r in range(self.table.rowCount()):
            name = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
            cmd = (self.table.item(r, 1).text() if self.table.item(r, 1) else "").strip()
            terminal = self.table.item(r, 2).checkState() == Qt.Checked
            if keep_empty or (name and cmd):
                out.append({"name": name, "command": cmd, "terminal": terminal})
        return out


# ---------------------------------------------------------------- settings dialog

STARTUP_CHOICES = [("restore", "Reopen the tabs from last time"), ("home", "Your home folder"),
                   ("folder", "This folder:")]


class SettingsDialog(QDialog):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.window_ = window
        self.setWindowTitle(f"{APP_NAME} Settings")
        self.setMinimumWidth(520)
        s = window.settings
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        # Startup
        self.startup_group = QButtonGroup(self)
        startup_box = QVBoxLayout()
        current = s.value("startup", "restore")
        for key, label in STARTUP_CHOICES:
            rb = QRadioButton(label)
            rb.setProperty("key", key)
            rb.setChecked(key == current)
            self.startup_group.addButton(rb)
            startup_box.addWidget(rb)
        folder_row = QHBoxLayout()
        self.startup_folder = QLineEdit(s.value("startup_folder", HOME))
        self.startup_folder.setCompleter(path_completer(self))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row.addSpacing(22)
        folder_row.addWidget(self.startup_folder, 1)
        folder_row.addWidget(browse)
        startup_box.addLayout(folder_row)
        form.addRow("When the app opens, show:", startup_box)

        # Layout
        self.layout_combo = QComboBox()
        for mode, label in VIEW_MODES:
            self.layout_combo.addItem(label, mode)
        self.layout_combo.setCurrentIndex(
            max(0, self.layout_combo.findData(window.default_view)))
        layout_row = QHBoxLayout()
        layout_row.addWidget(self.layout_combo)
        forget = QPushButton("Forget per-folder layouts")
        forget.setToolTip(f"{len(window.folder_views)} folders have their own layout")
        forget.clicked.connect(self._forget_layouts)
        layout_row.addWidget(forget)
        layout_row.addStretch()
        form.addRow("Default layout:", layout_row)

        # Toggles
        self.hidden = QCheckBox("Show hidden files")
        self.hidden.setChecked(window.show_hidden)
        self.preview = QCheckBox("Show the preview pane")
        self.preview.setChecked(window.act_preview.isChecked())
        self.dual = QCheckBox("Dual pane")
        self.dual.setChecked(window.dual)
        self.confirm_trash = QCheckBox("Ask before moving items to the Trash")
        self.confirm_trash.setChecked(window.confirm_trash)
        options = QVBoxLayout()
        for box in (self.hidden, self.preview, self.dual, self.confirm_trash):
            options.addWidget(box)
        form.addRow("Options:", options)
        commands_btn = QPushButton("Edit Custom Commands…")
        commands_btn.clicked.connect(window.edit_commands)
        commands_row = QHBoxLayout()
        commands_row.addWidget(commands_btn)
        commands_row.addStretch()
        form.addRow("Right-click menu:", commands_row)

        where = QLabel(f"Settings are saved automatically to:<br><small>"
                       f"{html.escape(s.fileName())}</small>")
        where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        where.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        reset = buttons.addButton("Reset All Settings…", QDialogButtonBox.ResetRole)
        reset.clicked.connect(self._reset)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(where)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose the startup folder",
                                                  self.startup_folder.text() or HOME)
        if folder:
            self.startup_folder.setText(folder)
            self.startup_group.buttons()[2].setChecked(True)

    def _forget_layouts(self) -> None:
        self.window_.folder_views.clear()
        self.window_.schedule_save()
        self.sender().setToolTip("0 folders have their own layout")
        self.sender().setEnabled(False)

    def _reset(self) -> None:
        answer = QMessageBox.question(
            self, APP_NAME, "Reset all settings to their defaults?\n\nThis resets favorites, "
                            "layouts, tabs and window size.")
        if answer == QMessageBox.Yes:
            self.window_.reset_settings()
            self.reject()

    def _save(self) -> None:
        w, s = self.window_, self.window_.settings
        startup = self.startup_group.checkedButton().property("key")
        folder = os.path.expanduser(self.startup_folder.text().strip())
        if startup == "folder" and not os.path.isdir(folder):
            QMessageBox.warning(self, APP_NAME, f"The folder “{folder}” doesn't exist.")
            return
        s.setValue("startup", startup)
        s.setValue("startup_folder", folder)
        w.default_view = self.layout_combo.currentData()
        w.confirm_trash = self.confirm_trash.isChecked()
        w.act_hidden.setChecked(self.hidden.isChecked())
        w.act_preview.setChecked(self.preview.isChecked())
        w.act_dual.setChecked(self.dual.isChecked())
        # Apply the new default to open tabs showing folders without their own layout
        for pane in w.panes:
            for t in pane.all_tabs():
                t.set_view_mode(w._mode_for(t.path))
        w._sync_view_actions()
        w.save_settings()
        self.accept()


# ---------------------------------------------------------------- main window

DEFAULT_FAVORITES = [os.path.join(HOME, d) for d in ("", "Desktop", "Documents", "Downloads")] + [
    "/Applications"]


def _as_list(value) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(SETTINGS_SCOPE, APP_NAME)
        migrate_legacy_settings(self.settings)
        self.show_hidden = self.settings.value("show_hidden", False, type=bool)
        self.default_view = self.settings.value("view_mode", "details")
        try:
            self.folder_views: dict[str, str] = json.loads(
                self.settings.value("folder_views", "{}"))
        except (TypeError, ValueError):
            self.folder_views = {}
        self.confirm_trash = self.settings.value("confirm_trash", False, type=bool)
        try:
            self.custom_commands: list[dict] = json.loads(self.settings.value(
                "custom_commands", json.dumps(DEFAULT_COMMANDS)))
        except (TypeError, ValueError):
            self.custom_commands = list(DEFAULT_COMMANDS)
        self._procs: list[QProcess] = []
        self.dual = False
        self.clip: tuple[str, list[str]] | None = None
        self._properties: list[PropertiesDialog] = []
        # Settings are written shortly after every change (and on quit), not only on close.
        self._save_timer = QTimer(self, singleShot=True, interval=500)
        self._save_timer.timeout.connect(self.save_settings)
        self._preview_timer = QTimer(self, singleShot=True,
                                     interval=QApplication.doubleClickInterval() // 2 + 50)
        self._preview_timer.timeout.connect(self._update_preview)
        self.ops = FileOps(self)
        self.ops.done.connect(self._on_ops_done)

        favs = _as_list(self.settings.value("favorites")) or [p.rstrip("/") for p in
                                                              DEFAULT_FAVORITES]
        self.sidebar = Sidebar(favs)
        self.sidebar.setMinimumWidth(150)
        self.sidebar.navigate_requested.connect(self.go_to)
        self.sidebar.favorites_changed.connect(self.schedule_save)

        self.panes = [Pane(self), Pane(self)]
        self.active = self.panes[0]
        self.pane_split = QSplitter(Qt.Horizontal)
        for p in self.panes:
            self.pane_split.addWidget(p)
        self.preview = PreviewPanel()
        self.main_split = QSplitter(Qt.Horizontal)
        self.main_split.addWidget(self.sidebar)
        self.main_split.addWidget(self.pane_split)
        self.main_split.addWidget(self.preview)
        self.main_split.setStretchFactor(1, 1)
        self.main_split.setSizes([190, 760, 280])
        self.setCentralWidget(self.main_split)

        self.status_label = QLabel()
        self.statusBar().addWidget(self.status_label)
        self.status_view_buttons = {}
        for mode, glyph in (("details", "☰"), ("large", "▦")):
            b = QToolButton(text=glyph)
            b.setToolTip(VIEW_LABELS[mode])
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.clicked.connect(lambda _=False, m=mode: self.set_view_mode(m))
            self.statusBar().addPermanentWidget(b)
            self.status_view_buttons[mode] = b

        self._build_actions()
        self._build_toolbar()
        self._build_menus()
        QApplication.instance().focusChanged.connect(self._on_focus_changed)

        startup = self.settings.value("startup", "restore")
        folder = self.settings.value("startup_folder", HOME)
        for i, pane in enumerate(self.panes):
            if startup == "restore":
                paths = _as_list(self.settings.value(f"tabs{i}")) or [HOME]
            else:
                paths = [folder if startup == "folder" and os.path.isdir(folder) else HOME]
            for path in paths:
                self.add_tab(pane, path)
        self.act_hidden.setChecked(self.show_hidden)
        self.act_dual.setChecked(self.settings.value("dual", False, type=bool))
        self.set_dual(self.act_dual.isChecked())
        self.act_preview.setChecked(self.settings.value("preview", True, type=bool))
        self.preview.setVisible(self.act_preview.isChecked())

        if geo := self.settings.value("geometry"):
            self.restoreGeometry(geo)
        else:
            self.resize(1250, 760)
        if state := self.settings.value("main_split"):
            self.main_split.restoreState(state)
        if state := self.settings.value("pane_split"):
            self.pane_split.restoreState(state)
        self.set_active(self.panes[0])
        self.tab().view.setFocus()
        for split in (self.main_split, self.pane_split):
            split.splitterMoved.connect(lambda *_: self.schedule_save())
        QApplication.instance().aboutToQuit.connect(self.save_settings)

    # -- actions, toolbar, menus

    def _act(self, text, slot, shortcut=None, icon=None, checkable=False) -> QAction:
        a = QAction(text, self)
        if icon is not None:
            a.setIcon(self.style().standardIcon(icon))
        if shortcut:
            shortcuts = shortcut if isinstance(shortcut, (list, tuple)) else [shortcut]
            a.setShortcuts([QKeySequence(s) for s in shortcuts])
        if checkable:
            a.setCheckable(True)
            a.toggled.connect(slot)
        else:
            a.triggered.connect(lambda _=False: slot())
        self.addAction(a)
        return a

    def _build_actions(self) -> None:
        S = QStyle.StandardPixmap
        A = self._act
        # Qt maps "Ctrl" to ⌘ and "Meta" to ⌃ on macOS.
        self.act_back = A("Back", lambda: self.tab().go_back(), "Ctrl+[", S.SP_ArrowBack)
        self.act_fwd = A("Forward", lambda: self.tab().go_forward(), "Ctrl+]", S.SP_ArrowForward)
        self.act_up = A("Enclosing Folder", lambda: self.tab().go_up(), "Ctrl+Up", S.SP_ArrowUp)
        self.act_home = A("Home", lambda: self.go_to(HOME), "Ctrl+Shift+H")
        self.act_desktop = A("Desktop", lambda: self.go_to(f"{HOME}/Desktop"), "Ctrl+Shift+D")
        self.act_docs = A("Documents", lambda: self.go_to(f"{HOME}/Documents"), "Ctrl+Shift+O")
        self.act_downloads = A("Downloads", lambda: self.go_to(f"{HOME}/Downloads"),
                               "Ctrl+Alt+L")
        self.act_apps = A("Applications", lambda: self.go_to("/Applications"), "Ctrl+Shift+A")
        self.act_goto = A("Go to Folder…", self.focus_path_bar, ["Ctrl+Shift+G", "Ctrl+L"])

        self.act_new_tab = A("New Tab", lambda: self.new_tab(), "Ctrl+T")
        self.act_close_tab = A("Close Tab", self.close_current_tab, "Ctrl+W")
        self.act_next_tab = A("Next Tab", lambda: self.cycle_tab(1), "Meta+Tab")
        self.act_prev_tab = A("Previous Tab", lambda: self.cycle_tab(-1), "Meta+Shift+Tab")
        self.act_new_folder = A("New Folder", self.new_folder, "Ctrl+Shift+N",
                                S.SP_FileDialogNewFolder)
        self.act_open = A("Open", lambda: self.tab().open_selection(), ["Ctrl+O", "Ctrl+Down"])
        self.act_quicklook = A("Quick Look", self.quick_look, "Ctrl+Y")
        self.act_trash = A("Move to Trash", self.trash, "Ctrl+Backspace", S.SP_TrashIcon)
        self.act_delete = A("Delete Immediately…", self.delete_permanently, "Ctrl+Alt+Backspace")
        self.act_reveal = A("Show in Finder", self.reveal_in_finder, "Ctrl+Alt+R")
        self.act_terminal = A("Open in Terminal", self.open_terminal, "Ctrl+Alt+T")
        self.act_add_fav = A("Add to Sidebar", self.add_to_sidebar, "Meta+Ctrl+T")

        self.act_cut = A("Cut", self.cut, "Ctrl+X")
        self.act_copy = A("Copy", self.copy, "Ctrl+C")
        self.act_paste = A("Paste", self.paste, "Ctrl+V")
        self.act_duplicate = A("Duplicate", self.duplicate, "Ctrl+D")
        self.act_rename = A("Rename", self.rename, "F2")
        self.act_properties = A("Properties", self.show_properties, ["Ctrl+I", "Alt+Return"],
                                S.SP_FileDialogInfoView)
        self.act_settings = A("Settings…", self.open_settings, "Ctrl+,")
        self.act_settings.setMenuRole(QAction.PreferencesRole)  # → app menu on macOS
        self.act_edit_commands = A("Edit Custom Commands…", self.edit_commands)
        self.act_copy_path = A("Copy Path", self.copy_path, "Ctrl+Alt+C")
        self.act_copy_name = A("Copy Name", self.copy_name, "Ctrl+Alt+Shift+C")
        self.act_copy_folder_path = A("Copy Path of This Folder",
                                      lambda: self.tab().copy_folder_path())
        self.act_select_all = A("Select All", lambda: self.tab().view.selectAll(), "Ctrl+A")
        self.act_copy_other = A("Copy to Other Pane", lambda: self.to_other_pane("copy"), "F5")
        self.act_move_other = A("Move to Other Pane", lambda: self.to_other_pane("move"), "F6")
        self.act_find = A("Filter…", self.focus_search, "Ctrl+F")

        self.act_hidden = A("Show Hidden Files", self.set_show_hidden,
                            ["Ctrl+Shift+.", "Ctrl+>"], checkable=True)
        self.act_dual = A("Dual Pane", self.set_dual, "Ctrl+Alt+D", checkable=True)
        self.act_preview = A("Show Preview", self.set_preview, "Ctrl+Shift+P", checkable=True)

        # Layouts: ⌘1 … ⌘8, in Windows order
        self.view_group = QActionGroup(self)
        self.view_actions: dict[str, QAction] = {}
        for n, (mode, label) in enumerate(VIEW_MODES, start=1):
            a = A(label, lambda m=mode: self.set_view_mode(m), f"Ctrl+{n}")
            a.setCheckable(True)
            self.view_group.addAction(a)
            self.view_actions[mode] = a

        # Sort by (needed in icon layouts, which have no column headers)
        self.sort_group = QActionGroup(self)
        self.sort_actions: dict[int, QAction] = {}
        for col, label in ((0, "Name"), (3, "Date modified"), (2, "Type"), (1, "Size")):
            a = A(label, lambda c=col: self.tab().sort_by(c))
            a.setCheckable(True)
            self.sort_group.addAction(a)
            self.sort_actions[col] = a
        self.order_group = QActionGroup(self)
        self.act_asc = A("Ascending", lambda: self.tab().sort_by(order=Qt.AscendingOrder))
        self.act_desc = A("Descending", lambda: self.tab().sort_by(order=Qt.DescendingOrder))
        for a in (self.act_asc, self.act_desc):
            a.setCheckable(True)
            self.order_group.addAction(a)

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Navigation")
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.addAction(self.act_new_folder)
        view_btn = QToolButton(text="View")
        view_btn.setToolTip("Change layout (⌘1–⌘8, or ⌘ + scroll)")
        view_btn.setPopupMode(QToolButton.InstantPopup)
        view_btn.setMenu(self._view_menu(include_window_options=False))
        tb.addWidget(view_btn)
        tb.addAction(self.act_rename)
        tb.addAction(self.act_properties)
        self.act_hidden.setIconText("Hidden Files")
        self.act_hidden.setToolTip("Show hidden files (⇧⌘.)")
        tb.addAction(self.act_hidden)
        tb.addSeparator()
        for act, label in ((self.act_dual, "Dual"), (self.act_preview, "Preview")):
            act.setIconText(label)
            tb.addAction(act)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter this folder  ⌘F")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(210)
        self.search.textChanged.connect(self._on_search)
        self.search.returnPressed.connect(lambda: self.tab().view.setFocus())
        tb.addWidget(self.search)
        tb.addAction(self.act_settings)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        sections = {
            "File": [self.act_new_tab, self.act_new_folder, self.act_open, self.act_quicklook,
                     self.act_properties, self.act_settings,
                     self.act_close_tab, None, self.act_trash, self.act_delete, None,
                     self.act_add_fav, self.act_reveal, self.act_terminal],
            "Edit": [self.act_cut, self.act_copy, self.act_paste, self.act_duplicate,
                     self.act_rename, self.act_copy_path, self.act_copy_name,
                     self.act_copy_folder_path, self.act_select_all, None,
                     self.act_copy_other, self.act_move_other, None, self.act_find],
            "Go": [self.act_back, self.act_fwd, self.act_up, None, self.act_home,
                   self.act_desktop, self.act_docs, self.act_downloads, self.act_apps, None,
                   self.act_goto],
        }
        sections = dict(list(sections.items())[:2]) | {"View": None} | dict(
            list(sections.items())[2:])
        for title, actions in sections.items():
            if actions is None:
                mb.addMenu(self._view_menu(include_window_options=True))
                continue
            menu = mb.addMenu(title)
            for a in actions:
                menu.addSeparator() if a is None else menu.addAction(a)
        commands_menu = mb.addMenu("Commands")
        commands_menu.aboutToShow.connect(lambda m=commands_menu: (m.clear(),
                                                                    self._add_command_items(m)))

    def _sort_menu(self, parent: QMenu) -> QMenu:
        menu = QMenu("Sort by", parent)
        menu.addActions(list(self.sort_actions.values()))
        menu.addSeparator()
        menu.addActions([self.act_asc, self.act_desc])
        return menu

    def _view_menu(self, include_window_options: bool) -> QMenu:
        menu = QMenu("View", self)
        menu.addActions(list(self.view_actions.values()))
        menu.addSeparator()
        menu.addMenu(self._sort_menu(menu))
        menu.addSeparator()
        menu.addAction(self.act_hidden)
        menu.addAction(self.act_preview)
        if include_window_options:
            menu.addAction(self.act_dual)
            menu.addSeparator()
            menu.addActions([self.act_next_tab, self.act_prev_tab])
        menu.aboutToShow.connect(self._sync_view_actions)
        return menu

    def _sync_view_actions(self) -> None:
        tab = self.tab()
        if tab is None:
            return
        self.view_actions[tab.mode].setChecked(True)
        for mode, b in self.status_view_buttons.items():
            b.setChecked(tab.mode == mode)
        h = tab.tree.header()
        if a := self.sort_actions.get(h.sortIndicatorSection()):
            a.setChecked(True)
        (self.act_asc if h.sortIndicatorOrder() == Qt.AscendingOrder else self.act_desc
         ).setChecked(True)

    # -- layouts

    def _mode_for(self, path: str) -> str:
        return self.folder_views.get(path, self.default_view)

    def set_view_mode(self, mode: str) -> None:
        """Change the current folder's layout; it is remembered for this folder, and
        becomes the default for folders that have no layout of their own yet."""
        tab = self.tab()
        tab.set_view_mode(mode)
        self.folder_views.pop(tab.path, None)
        self.folder_views[tab.path] = mode
        while len(self.folder_views) > 1000:
            self.folder_views.pop(next(iter(self.folder_views)))
        self.default_view = mode
        self._sync_view_actions()
        self.schedule_save()

    def zoom(self, step: int) -> None:
        """step > 0: bigger (towards Extra large icons); step < 0: towards Content."""
        order = [m for m, _ in VIEW_MODES]
        i = order.index(self.tab().mode)
        new = order[max(0, min(len(order) - 1, i - step))]
        if new != self.tab().mode:
            self.set_view_mode(new)
            self.statusBar().showMessage(VIEW_LABELS[new], 1500)

    # -- tabs & panes

    def tab(self) -> BrowserTab:
        return self.active.current()

    def other_pane(self) -> Pane | None:
        if not self.dual:
            return None
        return self.panes[1] if self.active is self.panes[0] else self.panes[0]

    def add_tab(self, pane: Pane, path: str) -> BrowserTab:
        tab = BrowserTab(path, self.show_hidden)
        tab.location_changed.connect(self._on_location_changed)
        tab.selection_changed.connect(self._on_selection_changed)
        tab.contents_changed.connect(self._on_contents_changed)
        tab.new_tab_requested.connect(lambda p, pn=pane: self.add_tab(pn, p))
        tab.drop_requested.connect(self._run_op)
        tab.open_failed.connect(self._show_open_failed)
        tab.message.connect(lambda m: self.statusBar().showMessage(m, 4000))
        tab.context_menu_requested.connect(lambda pos, t=tab: self._context_menu(t, pos))
        tab.quicklook_requested.connect(self.quick_look)
        tab.switch_pane_requested.connect(self.switch_pane)
        tab.zoom_requested.connect(self.zoom)
        tab.tree.header().sortIndicatorChanged.connect(lambda *_: self._sync_view_actions())
        tab.set_view_mode(self._mode_for(tab.path))
        i = pane.tabs.addTab(tab, display_name(tab.path))
        pane.tabs.setTabToolTip(i, tab.path)
        pane.tabs.setCurrentIndex(i)
        return tab

    def new_tab(self, pane: Pane | None = None, path: str | None = None) -> None:
        pane = pane or self.active
        cur = pane.current()
        tab = self.add_tab(pane, path or (cur.path if cur else HOME))
        self.set_active(pane)
        tab.view.setFocus()

    def close_tab(self, pane: Pane, index: int) -> None:
        if pane.tabs.count() == 1:
            if pane is self.panes[1]:
                self.act_dual.setChecked(False)
            else:
                self.close()
            return
        w = pane.tabs.widget(index)
        pane.tabs.removeTab(index)
        w.deleteLater()
        self.schedule_save()

    def close_current_tab(self) -> None:
        self.close_tab(self.active, self.active.tabs.currentIndex())

    def cycle_tab(self, step: int) -> None:
        tabs = self.active.tabs
        tabs.setCurrentIndex((tabs.currentIndex() + step) % tabs.count())
        self.tab().view.setFocus()

    def on_tab_switched(self, pane: Pane) -> None:
        if pane is self.active and pane.current():
            self._sync_ui()

    def set_dual(self, on: bool) -> None:
        self.dual = on
        self.panes[1].setVisible(on)
        if on and self.panes[1].tabs.count() == 0:
            self.add_tab(self.panes[1], self.tab().path)
        if on:
            half = max(1, sum(self.pane_split.sizes()) // 2)
            self.pane_split.setSizes([half, half])
        if not on and self.active is self.panes[1]:
            self.set_active(self.panes[0])
            self.tab().view.setFocus()
        self._update_pane_borders()
        self.schedule_save()

    def switch_pane(self) -> None:
        if other := self.other_pane():
            other.current().view.setFocus()

    def set_active(self, pane: Pane) -> None:
        self.active = pane
        self._update_pane_borders()
        self._sync_ui()

    def _update_pane_borders(self) -> None:
        for p in self.panes:
            p.setProperty("active", self.dual and p is self.active)
            p.style().unpolish(p)
            p.style().polish(p)

    def _on_focus_changed(self, _old, new) -> None:
        w = new
        while w is not None:
            if isinstance(w, Pane):
                if w is not self.active:
                    self.set_active(w)
                return
            w = w.parentWidget()

    # -- UI sync

    def _sync_ui(self) -> None:
        tab = self.tab()
        if tab is None:
            return
        self.search.blockSignals(True)
        self.search.setText(tab.proxy.text)
        self.search.blockSignals(False)
        self.act_back.setEnabled(bool(tab.back))
        self.act_fwd.setEnabled(bool(tab.forward))
        self.act_up.setEnabled(tab.path != "/")
        self.setWindowTitle(display_name(tab.path))
        self.setWindowFilePath(tab.path)  # proxy icon in the title bar
        self.sidebar.highlight(tab.path)
        self._sync_view_actions()
        self._update_preview()
        self._update_status()

    def _on_location_changed(self, tab: BrowserTab) -> None:
        self.schedule_save()
        if (mode := self._mode_for(tab.path)) != tab.mode:
            tab.set_view_mode(mode)
        for pane in self.panes:
            i = pane.tabs.indexOf(tab)
            if i >= 0:
                pane.tabs.setTabText(i, display_name(tab.path))
                pane.tabs.setTabToolTip(i, tab.path)
        if tab is self.tab():
            self._sync_ui()

    def _on_selection_changed(self, tab: BrowserTab) -> None:
        if tab is self.tab():
            # Loading a preview (big image, large folder) can take a moment; doing it right
            # after the first click of a double-click would make the second click lag.
            self._preview_timer.start()
            self._update_status()

    def _on_contents_changed(self, tab: BrowserTab) -> None:
        if tab is self.tab():
            self._update_status()

    def _update_preview(self) -> None:
        if not self.preview.isVisible():
            return
        paths = self.tab().selected_paths()
        self.preview.show_path(paths[0] if len(paths) == 1 else None)

    def _update_status(self) -> None:
        tab = self.tab()
        n = tab.item_count()
        text = f"{n} item{'s' if n != 1 else ''}"
        if sel := tab.view.selectionModel().selectedRows(0):
            text += f", {len(sel)} selected"
            if size := tab.selected_size():
                text += f" ({human_size(size)})"
        free = QStorageInfo(tab.path).bytesAvailable()
        if free > 0:
            text += f"  ·  {human_size(free)} available"
        self.status_label.setText(text)

    # -- navigation

    def go_to(self, path: str) -> None:
        self.tab().go_to(path)

    def _show_open_failed(self, path: str) -> None:
        QMessageBox.information(
            self, APP_NAME, f"The folder “{display_name(path)}” can't be opened because you "
                            "don't have permission to see its contents.")

    def focus_path_bar(self) -> None:
        self.tab().path_edit.setFocus()
        self.tab().path_edit.selectAll()

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _on_search(self, text: str) -> None:
        self.tab().proxy.set_text(text)
        self._update_status()

    def set_show_hidden(self, on: bool) -> None:
        self.show_hidden = on
        for pane in self.panes:
            for t in pane.all_tabs():
                t.set_show_hidden(on)
        self.schedule_save()

    def set_preview(self, on: bool) -> None:
        self.preview.setVisible(on)
        if on:
            self._update_preview()
        self.schedule_save()

    # -- file operations

    def _targets(self) -> list[str]:
        return self.tab().selected_paths()

    def _run_op(self, op: str, paths: list[str], dest: str) -> None:
        if paths:
            self.statusBar().showMessage(f"{'Moving' if op == 'move' else 'Copying'}…")
            self.ops.start(op, paths, dest)

    def _on_ops_done(self, summary: str, errors: list) -> None:
        self.statusBar().showMessage(summary, 4000)
        if errors:
            more = f"\n…and {len(errors) - 10} more" if len(errors) > 10 else ""
            QMessageBox.warning(self, APP_NAME, "Some items failed:\n\n" +
                                "\n".join(errors[:10]) + more)

    def new_folder(self) -> None:
        tab = self.tab()
        self.search.clear()
        name = unique_dest(Path(tab.path), "untitled folder").name
        idx = tab.model.mkdir(tab.model.index(tab.path), name)
        if not idx.isValid():
            QMessageBox.warning(self, APP_NAME, f"Couldn't create a folder in “{tab.path}”.")
            return
        pidx = tab.proxy.mapFromSource(idx)
        tab.view.setFocus()
        tab.view.setCurrentIndex(pidx)
        tab.view.scrollTo(pidx)
        tab.view.edit(pidx)

    def rename(self) -> None:
        tab = self.tab()
        paths = tab.selected_paths()
        if len(paths) > 1:
            self.batch_rename(paths)
            return
        idx = tab.view.currentIndex()
        if paths and not tab.view.selectionModel().isSelected(idx.siblingAtColumn(0)):
            idx = tab.proxy.mapFromSource(tab.model.index(paths[0]))
        if idx.isValid():
            tab.view.setFocus()
            tab.view.edit(idx.siblingAtColumn(0))

    def batch_rename(self, paths: list[str]) -> None:
        """Windows-style: 'Holiday' → 'Holiday (1).jpg', 'Holiday (2).jpg', …"""
        base, ok = QInputDialog.getText(
            self, "Rename Items", f"New name for the {len(paths)} selected items.\n"
                                  "They will be numbered: Name (1), Name (2), …",
            text=Path(paths[0]).stem)
        base = base.strip()
        if not ok or not base:
            return
        if "/" in base:
            QMessageBox.warning(self, APP_NAME, f"“{base}” isn't a valid name.")
            return
        tab = self.tab()
        ordered = sorted(paths, key=lambda p: tab.proxy.mapFromSource(tab.model.index(p)).row())
        errors, renamed = [], []
        for n, path in enumerate(ordered, start=1):
            suffix = "" if os.path.isdir(path) else Path(path).suffix
            new_path = os.path.join(os.path.dirname(path), f"{base} ({n}){suffix}")
            try:
                if os.path.lexists(new_path):
                    raise OSError("an item with that name already exists")
                os.rename(path, new_path)
                renamed.append(new_path)
            except OSError as e:
                errors.append(f"{os.path.basename(path)}: {e}")
        self._on_ops_done(f"Renamed {len(renamed)} item(s)", errors)

    def show_properties(self) -> None:
        paths = self._targets() or [self.tab().path]
        dialog = PropertiesDialog(paths, self)
        self._properties.append(dialog)
        dialog.destroyed.connect(lambda *_, d=dialog: self._properties.remove(d)
                                 if d in self._properties else None)
        dialog.show()

    def _add_command_items(self, menu: QMenu) -> None:
        for cmd in self.custom_commands:
            label = cmd["name"] + ("  (Terminal)" if cmd.get("terminal") else "")
            a = menu.addAction(label, lambda c=cmd: self.run_custom_command(c))
            a.setToolTip(cmd["command"])
        menu.addAction(self.act_edit_commands)

    def run_custom_command(self, cmd: dict) -> None:
        tab = self.tab()
        folder = tab.path
        line = expand_command(cmd["command"], folder, tab.selected_paths())
        if cmd.get("terminal"):
            script = f"cd {shlex.quote(folder)} && {line}"
            subprocess.Popen(["osascript", "-e", 'tell application "Terminal"', "-e", "activate",
                              "-e", f"do script {applescript_string(script)}",
                              "-e", "end tell"])
            self.statusBar().showMessage(f"Running “{cmd['name']}” in Terminal", 3000)
            return
        proc = QProcess(self)
        proc.setWorkingDirectory(folder)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PATH", ":".join(EXTRA_PATH + [env.value("PATH", "/usr/bin:/bin")]))
        proc.setProcessEnvironment(env)
        proc.finished.connect(lambda code, _status, p=proc, c=cmd: self._command_finished(p, c, code))
        proc.errorOccurred.connect(lambda _err, p=proc, c=cmd: self._command_finished(p, c, -1))
        self._procs.append(proc)
        proc.start(os.environ.get("SHELL", "/bin/zsh"), ["-l", "-c", line])
        self.statusBar().showMessage(f"Running “{cmd['name']}”…", 3000)

    def _command_finished(self, proc: QProcess, cmd: dict, code: int) -> None:
        if proc not in self._procs:
            return  # already reported (errorOccurred and finished can both fire)
        self._procs.remove(proc)
        proc.deleteLater()
        if code == 0:
            self.statusBar().showMessage(f"“{cmd['name']}” finished", 3000)
            return
        err = bytes(proc.readAllStandardError()).decode(errors="replace").strip()
        out = bytes(proc.readAllStandardOutput()).decode(errors="replace").strip()
        detail = (err or out or proc.errorString())[-1500:]
        hint = ""
        if "command not found" in detail or "not found" in detail:
            hint = ("\n\nTip: the program isn't installed or isn't on your PATH. For VS Code, "
                    "open it and run “Shell Command: Install 'code' command in PATH” from the "
                    "Command Palette — or use: open -a \"Visual Studio Code\" .")
        QMessageBox.warning(self, APP_NAME, f"“{cmd['name']}” failed (exit code {code}):"
                                            f"\n\n{detail}{hint}")

    def edit_commands(self) -> None:
        dialog = CommandsDialog(self.custom_commands, self)
        if dialog.exec() == QDialog.Accepted:
            self.custom_commands = dialog.commands()
            self.save_settings()

    def open_settings(self) -> None:
        SettingsDialog(self).exec()

    def trash(self) -> None:
        paths = self._targets()
        if not paths:
            return
        if self.confirm_trash:
            what = f"“{os.path.basename(paths[0])}”" if len(paths) == 1 else \
                f"these {len(paths)} items"
            if QMessageBox.question(self, APP_NAME, f"Move {what} to the Trash?") != \
                    QMessageBox.Yes:
                return
        failed = [p for p in paths if not move_to_trash(p)]
        moved = len(paths) - len(failed)
        self.statusBar().showMessage(f"Moved {moved} item{'s' if moved != 1 else ''} to Trash",
                                     4000)
        if failed:
            QMessageBox.warning(self, APP_NAME, "Couldn't move to Trash:\n\n" +
                                "\n".join(os.path.basename(p) for p in failed))

    def delete_permanently(self) -> None:
        paths = self._targets()
        if not paths:
            return
        what = f"“{os.path.basename(paths[0])}”" if len(paths) == 1 else f"these {len(paths)} items"
        answer = QMessageBox.warning(
            self, APP_NAME, f"Are you sure you want to delete {what} immediately?\n\n"
                            "You can't undo this action.",
            QMessageBox.Delete | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Delete:
            return
        errors = []
        for p in paths:
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)
                else:
                    os.unlink(p)
            except OSError as e:
                errors.append(f"{os.path.basename(p)}: {e}")
        self._on_ops_done(f"Deleted {len(paths) - len(errors)} item(s)", errors)

    def _set_clipboard(self, op: str, paths: list[str]) -> None:
        self.clip = (op, paths)
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(p) for p in paths])
        md.setText("\n".join(paths))
        QGuiApplication.clipboard().setMimeData(md)

    def copy(self) -> None:
        if paths := self._targets():
            self._set_clipboard("copy", paths)
            self.statusBar().showMessage(f"Copied {len(paths)} item(s)", 3000)

    def cut(self) -> None:
        if paths := self._targets():
            self._set_clipboard("move", paths)
            self.statusBar().showMessage(f"Cut {len(paths)} item(s) — paste to move", 3000)

    def paste(self) -> None:
        md = QGuiApplication.clipboard().mimeData()
        if md is None:
            return
        sys_paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        if sys_paths:
            # Use our own clipboard (it remembers cut vs copy) if it still matches what's on
            # the system clipboard; otherwise the files came from elsewhere, e.g. Finder.
            if self.clip and sys_paths == self.clip[1]:
                op, paths = self.clip
            else:
                op, paths = "copy", sys_paths
            if op == "move":
                self.clip = None
            self._run_op(op, paths, self.tab().path)
            return
        # Plain text that is a path (e.g. from Copy Path or a terminal): go there.
        text = md.text() if md.hasText() else ""
        if parse_path(text):
            self.tab().go_to(text)
        elif text.strip():
            self.statusBar().showMessage("The clipboard doesn't contain files or a path", 4000)

    def duplicate(self) -> None:
        self._run_op("copy", self._targets(), self.tab().path)

    def to_other_pane(self, op: str) -> None:
        other = self.other_pane()
        if other is None:
            self.statusBar().showMessage("Turn on Dual Pane (⌥⌘D) to use this", 3000)
            return
        self._run_op(op, self._targets(), other.current().path)

    def _copy_text(self, items: list[str], what: str) -> None:
        QGuiApplication.clipboard().setText("\n".join(items))
        msg = f"Copied {what}: {items[0]}" if len(items) == 1 else f"Copied {len(items)} {what}s"
        self.statusBar().showMessage(msg, 4000)

    def copy_path(self) -> None:
        self._copy_text(self._targets() or [self.tab().path], "path")

    def copy_name(self) -> None:
        paths = self._targets() or [self.tab().path]
        self._copy_text([display_name(p) for p in paths], "name")

    def quick_look(self) -> None:
        if paths := self._targets():
            QProcess.startDetached("/usr/bin/qlmanage", ["-p", *paths])

    def reveal_in_finder(self) -> None:
        paths = self._targets()
        if paths:
            subprocess.Popen(["open", "-R", *paths])
        else:
            subprocess.Popen(["open", self.tab().path])

    def open_terminal(self) -> None:
        paths = self._targets()
        target = paths[0] if len(paths) == 1 and os.path.isdir(paths[0]) else self.tab().path
        subprocess.Popen(["open", "-a", "Terminal", target])

    def add_to_sidebar(self) -> None:
        dirs = [p for p in self._targets() if os.path.isdir(p)] or [self.tab().path]
        for d in dirs:
            self.sidebar.add_favorite(d)

    # -- context menu

    def _context_menu(self, tab: BrowserTab, pos) -> None:
        idx = tab.view.indexAt(pos)
        sm = tab.view.selectionModel()
        if not idx.isValid():
            tab.view.clearSelection()
        elif not sm.isRowSelected(idx.row(), idx.parent()):
            # macOS opens the menu on mouse-down, before the view has selected the item.
            sm.setCurrentIndex(idx, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        paths = tab.selected_paths()
        menu = QMenu(self)
        if not paths:
            self._add_command_items(menu)
            menu.addSeparator()
        if paths:
            menu.addAction(self.act_open)
            dirs = [p for p in paths if os.path.isdir(p) and not QFileInfo(p).isBundle()]
            if dirs:
                menu.addAction("Open in New Tab", lambda: [self.add_tab(self.active, d)
                                                           for d in dirs])
            if len(paths) == 1 and QFileInfo(paths[0]).isBundle():
                menu.addAction("Show Package Contents", lambda: tab.navigate(paths[0]))
            menu.addAction(self.act_quicklook)
            menu.addSeparator()
            for a in (self.act_rename, self.act_duplicate, self.act_trash):
                menu.addAction(a)
            menu.addSeparator()
            self._add_command_items(menu)
            menu.addSeparator()
            for a in (self.act_cut, self.act_copy, self.act_copy_path, self.act_copy_name):
                menu.addAction(a)
        menu.addAction(self.act_copy_folder_path)
        menu.addAction(self.act_paste)
        menu.addSeparator()
        if not paths:
            layouts = menu.addMenu("View")
            layouts.addActions(list(self.view_actions.values()))
            menu.addMenu(self._sort_menu(menu))
            menu.addAction(self.act_hidden)
            self._sync_view_actions()
            menu.addSeparator()
        menu.addAction(self.act_new_folder)
        if self.dual and paths:
            menu.addAction(self.act_copy_other)
            menu.addAction(self.act_move_other)
        menu.addSeparator()
        for a in (self.act_add_fav, self.act_reveal, self.act_terminal):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self.act_properties)
        menu.exec(tab.view.viewport().mapToGlobal(pos))

    # -- persistence

    def schedule_save(self) -> None:
        self._save_timer.start()

    def save_settings(self) -> None:
        self._save_timer.stop()
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("main_split", self.main_split.saveState())
        s.setValue("pane_split", self.pane_split.saveState())
        s.setValue("show_hidden", self.show_hidden)
        s.setValue("dual", self.dual)
        s.setValue("preview", self.act_preview.isChecked())
        s.setValue("view_mode", self.default_view)
        s.setValue("folder_views", json.dumps(self.folder_views))
        s.setValue("favorites", self.sidebar.favorites())
        s.setValue("confirm_trash", self.confirm_trash)
        s.setValue("custom_commands", json.dumps(self.custom_commands))
        for i, pane in enumerate(self.panes):
            s.setValue(f"tabs{i}", [t.path for t in pane.all_tabs()])
        s.sync()

    def reset_settings(self) -> None:
        self.settings.clear()
        self.folder_views.clear()
        self.default_view = "details"
        self.confirm_trash = False
        self.custom_commands = list(DEFAULT_COMMANDS)
        self.sidebar.set_favorites([p.rstrip("/") for p in DEFAULT_FAVORITES])
        self.act_hidden.setChecked(False)
        self.act_preview.setChecked(True)
        self.act_dual.setChecked(False)
        for pane in self.panes:
            for t in pane.all_tabs():
                t.set_view_mode("details")
        self._sync_view_actions()
        self.save_settings()
        self.statusBar().showMessage("Settings reset to defaults", 4000)

    def moveEvent(self, e):
        super().moveEvent(e)
        self.schedule_save()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.schedule_save()

    def closeEvent(self, e):
        self.save_settings()
        self.preview.cleanup()
        super().closeEvent(e)


class App(QApplication):
    """Opens folders dropped on the Dock icon or opened with “Open With → unfinder”."""

    window: "MainWindow | None" = None
    pending: list[str] = []

    def event(self, e):
        if e.type() == e.Type.FileOpen and e.file():
            if self.window is not None:
                self.window.go_to(e.file())
                self.window.raise_()
                self.window.activateWindow()
            else:
                self.pending.append(e.file())
            return True
        return super().event(e)


def main() -> None:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationDisplayName(APP_NAME)
    app = App(sys.argv)
    app.setStyleSheet(STYLE)
    icon = resource_path("assets/unfinder.png")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))  # the Dock icon when running from source
    window = app.window = MainWindow()
    # macOS can pass a -psn_… argument to apps started from Finder; ignore those.
    args = [a for a in sys.argv[1:] if not a.startswith("-psn")]
    for path in args + app.pending:
        window.go_to(path)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
