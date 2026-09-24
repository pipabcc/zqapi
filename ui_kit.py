"""本工具自带的轻量界面支撑件。

原来两个工具依赖主程序里的 ``settings_dialog`` / ``post_capture_actions`` / ``compact``，
这三个模块体量都在上百 KB 且各自拖着大量无关依赖，单独打包不划算。
这里用等价的轻量实现顶掉它们，只保留真正用到的三个能力：

* :func:`install_custom_text_context_menus` —— 中文右键菜单（原名来自 SettingsDialog）；
* :class:`SimpleMenuPopup` —— 圆角菜单弹窗（替代 OcrGenericMenuPopup）；
* :func:`ask_confirmation` —— 统一样式的「是 / 否」确认框。
"""

from __future__ import annotations

import ctypes
import os
from types import FunctionType
from typing import Any, Callable, Optional

from PyQt6.QtCore import QPoint, QPropertyAnimation, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app_paths import get_assets_dir


# 与主程序「实用工具」页保持同一套底色，移植过来时不用重新调色
MAIN_WINDOW_BACKGROUND = "#f4faff"
MAIN_WINDOW_BACKGROUND_COLORREF = 0x00FFFAF4
MODULE_BACKGROUND = "#fdfeff"

TEXT_WIDGET_TYPES = (QLineEdit, QTextEdit, QPlainTextEdit)

_MENU_ITEM_HEIGHT = 30
_MENU_SEPARATOR_HEIGHT = 6

# 同一个控件别重复挂右键菜单
_CONTEXT_MENU_FLAG = "zqapiCustomContextMenu"


# --------------------------------------------------------------------------- 资源


def asset_path(name: str) -> str:
    """``assets/`` 下的文件绝对路径（POSIX 风格，方便直接塞进 Qt 样式表 url()）。"""
    return (get_assets_dir() / str(name)).as_posix()


def load_icon(name: str) -> QIcon:
    path = asset_path(name)
    return QIcon(path) if os.path.isfile(path) else QIcon()


def load_pixmap(name: str, size: QSize | None = None) -> QPixmap:
    """按给定尺寸取图标位图；取不到就返回空位图（界面不会崩）。"""
    path = asset_path(name)
    if not os.path.isfile(path):
        return QPixmap()
    pixmap = QPixmap(path)
    if size is not None and not pixmap.isNull():
        pixmap = pixmap.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pixmap


def app_icon() -> QIcon:
    for name in ("app.ico", "icon_compare.svg"):
        icon = load_icon(name)
        if not icon.isNull():
            return icon
    return QIcon()


def dot_pixmap(color: str, size: int = 9) -> QPixmap:
    """历史列表行首的小圆点。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(0, 0, size - 1, size - 1)
    painter.end()
    return pixmap


def apply_caption_color(window: QWidget, colorref: int = MAIN_WINDOW_BACKGROUND_COLORREF) -> None:
    """把 Windows 标题栏染成窗口底色（非 Windows 或调用失败时静默跳过）。"""
    try:
        if os.name != "nt":
            return
        hwnd = int(window.winId())
        DWMWA_CAPTION_COLOR = 35
        color = ctypes.c_uint(colorref)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(DWMWA_CAPTION_COLOR),
            ctypes.byref(color),
            ctypes.sizeof(color),
        )
    except Exception:
        pass


# ------------------------------------------------------------------- 右键菜单弹窗


class SimpleMenuPopup(QWidget):
    """圆角白底菜单弹窗，用法与主程序的 OcrGenericMenuPopup 一致。

    ``items`` 是 ``(标题, 回调, 是否可用)`` 三元组列表，标题写 ``"-"`` 表示分隔线。
    """

    def __init__(
        self,
        items: list[tuple[str, Optional[Callable[[], None]], bool]],
        parent: QWidget | None = None,
        active_index: Optional[int] = None,
        **_ignored: Any,
    ) -> None:
        flags = Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        try:
            flags |= Qt.WindowType.NoDropShadowWindowHint
        except AttributeError:  # pragma: no cover - 老版本 Qt
            pass
        super().__init__(parent, flags)
        self.setObjectName("ZqapiMenuPopupRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._items = list(items or [])
        self._active_index = active_index
        self._build()

    # 圆角卡片 + 一圈浅边
    def paintEvent(self, event) -> None:  # noqa: D102 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(QPen(QColor("#dfe4ec"), 1))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)
        painter.end()
        super().paintEvent(event)

    def _build(self) -> None:
        metrics = QFontMetrics(self.font())
        width = 0
        height = 12
        for item in self._items:
            title = str(item[0])
            if title == "-":
                height += _MENU_SEPARATOR_HEIGHT
                continue
            height += _MENU_ITEM_HEIGHT
            width = max(width, int(metrics.horizontalAdvance(title)))
        width = max(120, width + 34)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(2)

        for index, item in enumerate(self._items):
            title = str(item[0])
            if title == "-":
                layout.addWidget(self._make_separator())
                continue
            callback = item[1] if len(item) > 1 else None
            enabled = bool(item[2]) if len(item) > 2 else True
            layout.addWidget(self._make_item(title, callback, enabled, index == self._active_index))

        self.setFixedSize(width, height)

    def _make_separator(self) -> QWidget:
        line = QWidget(self)
        line.setFixedHeight(1)
        line.setStyleSheet("background: #eef1f6; margin: 2px 6px;")
        wrapper = QWidget(self)
        wrapper.setFixedHeight(_MENU_SEPARATOR_HEIGHT)
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.addWidget(line)
        return wrapper

    def _make_item(
        self,
        title: str,
        callback: Optional[Callable[[], None]],
        enabled: bool,
        active: bool,
    ) -> QPushButton:
        button = QPushButton(title, self)
        button.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        button.setEnabled(enabled)
        button.setFixedHeight(_MENU_ITEM_HEIGHT)
        background = "#eef2f7" if active else "transparent"
        button.setStyleSheet(
            """
            QPushButton {
                text-align: left; padding: 0 12px; border: none; border-radius: 6px;
                background: __BG__; color: #334155; font-size: 13px; font-weight: 600;
            }
            QPushButton:hover { background: #f1f5f9; color: #0f172a; }
            QPushButton:pressed { background: #e2e8f0; color: #0f172a; }
            QPushButton:disabled { color: #cbd5e1; }
            """.replace("__BG__", background)
        )

        def trigger() -> None:
            self.close()
            if callback is None:
                return
            try:
                callback()
            except RuntimeError:  # pragma: no cover - 宿主控件已销毁
                pass

        button.clicked.connect(trigger)
        return button

    def _adjust_position(self, global_pos: QPoint) -> QPoint:
        pos = QPoint(int(global_pos.x()), int(global_pos.y()))
        try:
            screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        except Exception:  # pragma: no cover - 无屏幕环境
            screen = None
        if screen is None:
            return pos
        area = screen.availableGeometry()
        x = min(max(pos.x(), area.left()), max(area.left(), area.right() - self.width()))
        y = min(max(pos.y(), area.top()), max(area.top(), area.bottom() - self.height()))
        return QPoint(x, y)

    def show_at_pos(self, global_pos: QPoint) -> None:
        self.move(self._adjust_position(global_pos))
        self.show()
        self.raise_()
        self.activateWindow()


# --------------------------------------------------------------- 文本控件右键菜单


def _text_plain(widget: QWidget) -> str:
    if isinstance(widget, QLineEdit):
        return str(widget.text() or "")
    if isinstance(widget, (QTextEdit, QPlainTextEdit)):
        return str(widget.toPlainText() or "")
    return ""


def _has_selection(widget: QWidget) -> bool:
    if isinstance(widget, QLineEdit):
        if bool(widget.hasSelectedText()):
            return True
        start, end = widget.selectionStart(), widget.selectionEnd()
        return start >= 0 and end > start
    if isinstance(widget, (QTextEdit, QPlainTextEdit)):
        return bool(widget.textCursor().hasSelection())
    return False


def _selected_text(widget: QWidget) -> str:
    if isinstance(widget, QLineEdit):
        selected = str(widget.selectedText() or "")
        if selected:
            return selected
        start, end = widget.selectionStart(), widget.selectionEnd()
        if start >= 0 and end > start:
            return str(widget.text() or "")[start:end]
        return ""
    if isinstance(widget, (QTextEdit, QPlainTextEdit)):
        return str(widget.textCursor().selectedText() or "")
    return ""


def _delete_selection(widget: QWidget) -> None:
    if isinstance(widget, QLineEdit):
        widget.del_()
        return
    if isinstance(widget, (QTextEdit, QPlainTextEdit)):
        widget.textCursor().removeSelectedText()


def _is_read_only(widget: QWidget) -> bool:
    try:
        return bool(widget.isReadOnly())
    except AttributeError:
        return False


def _undo_available(widget: QWidget) -> bool:
    """QLineEdit 有 isUndoAvailable()，QTextEdit/QPlainTextEdit 要看 document()。"""
    if isinstance(widget, QLineEdit):
        return bool(widget.isUndoAvailable())
    document = widget.document() if hasattr(widget, "document") else None
    if document is not None and hasattr(document, "isUndoAvailable"):
        return bool(document.isUndoAvailable())
    return False


def _redo_available(widget: QWidget) -> bool:
    if isinstance(widget, QLineEdit):
        return bool(widget.isRedoAvailable())
    document = widget.document() if hasattr(widget, "document") else None
    if document is not None and hasattr(document, "isRedoAvailable"):
        return bool(document.isRedoAvailable())
    return False


def _clipboard_has_text() -> bool:
    app = QApplication.instance()
    if app is None or app.clipboard() is None:
        return False
    mime = app.clipboard().mimeData()
    if mime is None:
        return False
    return bool(mime.hasText())


def text_widget_menu_items(widget: QWidget) -> list[tuple[str, Optional[Callable[[], None]], bool]]:
    """标准中文右键菜单条目（与主程序 SettingsDialog 的条目保持一致）。"""
    read_only = _is_read_only(widget)
    has_selection = _has_selection(widget)
    has_text = bool(_text_plain(widget))

    def run(action: Callable[[], None]) -> Callable[[], None]:
        def wrapped() -> None:
            try:
                action()
            except RuntimeError:  # pragma: no cover - 控件已销毁
                pass

        return wrapped

    def copy_selected() -> None:
        text = _selected_text(widget)
        if not text:
            return
        app = QApplication.instance()
        if app is not None and app.clipboard() is not None:
            app.clipboard().setText(text)

    def cut_selected() -> None:
        copy_selected()
        _delete_selection(widget)

    return [
        ("撤销", run(widget.undo), _undo_available(widget) and not read_only),
        ("重做", run(widget.redo), _redo_available(widget) and not read_only),
        ("-", None, False),
        ("剪切", run(cut_selected), has_selection and not read_only),
        ("复制", run(copy_selected), has_selection),
        ("粘贴", run(widget.paste), _clipboard_has_text() and not read_only),
        ("删除", run(lambda: _delete_selection(widget)), has_selection and not read_only),
        ("-", None, False),
        ("全选", run(widget.selectAll), has_text),
    ]


def _iter_text_widgets(root: object) -> list[QWidget]:
    if isinstance(root, TEXT_WIDGET_TYPES):
        return [root]
    if isinstance(root, (list, tuple, set)):
        return [widget for widget in root if isinstance(widget, TEXT_WIDGET_TYPES)]
    if isinstance(root, QWidget):
        widgets: list[QWidget] = []
        if isinstance(root, TEXT_WIDGET_TYPES):
            widgets.append(root)
        widgets.extend(root.findChildren(TEXT_WIDGET_TYPES))
        return widgets
    return []


def install_custom_text_context_menus(owner: QWidget, root: object) -> None:
    """把 ``root`` 及其子树里的文本输入控件的右键菜单换成中文版。"""
    for widget in _iter_text_widgets(root):
        if widget is None:
            continue
        # 自己实现了 contextMenuEvent 的控件不要覆盖它的行为
        if isinstance(type(widget).__dict__.get("contextMenuEvent"), FunctionType):
            continue
        if bool(widget.property(_CONTEXT_MENU_FLAG)):
            continue
        widget.setProperty(_CONTEXT_MENU_FLAG, True)
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        def show_menu(pos: QPoint, target: QWidget = widget, host: QWidget = owner) -> None:
            popup = SimpleMenuPopup(text_widget_menu_items(target), parent=target)
            setattr(host, "_zqapi_context_menu_popup", popup)
            popup.show_at_pos(target.mapToGlobal(pos))

        widget.customContextMenuRequested.connect(show_menu)


# --------------------------------------------------------------------- 确认弹窗


def ask_confirmation(parent: QWidget, title: str, text: str, *, warning: bool = False) -> bool:
    """统一样式的「是 / 否」确认框，默认按钮是「否」，避免手滑。"""
    buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    ask = QMessageBox.warning if warning else QMessageBox.question
    answer = ask(parent, title, text, buttons, QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


# ----------------------------------------------------------------- 自适应省略标签


class ElidedLabel(QLabel):
    """宽度不够就把文字省略掉的 QLabel（悬停看完整内容）。

    QLabel 默认的 minimumSizeHint 等于整段文字的宽度，长文案要么把整行顶宽、要么直接溢出被裁。
    这里把水平尺寸策略设成 ``Ignored``（可以一直压到 0），宽度一变就重新省略 —— 于是它天然
    充当"吸收剩余空间"的那一栏，窄窗口下也不会把别的控件挤坏。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt 命名
        self._full_text = str(text or "")
        self._apply_elide()

    def text(self) -> str:  # noqa: N802 - Qt 命名
        return self._full_text

    def full_text(self) -> str:
        return self._full_text

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        """宽度上完全可以让步。

        QLabel 默认的最小宽度等于整段文字宽度，光把水平策略设成 Ignored 还不够 —— 布局仍然
        会按"最小宽度"给它留位置，结果是把同一行里别的控件（比如"显示"按钮）挤到换行。
        这里把最小宽度归零，让它老实当那条"吸收剩余空间"的伸缩缝。
        """
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = int(self.width())
        if width <= 0:
            super().setText("")
            return
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, width)
        super().setText(elided)
        self.setToolTip(self._full_text if elided != self._full_text else "")


# --------------------------------------------------------------------- 轻提示浮窗


class ToastMessage(QWidget):
    """圆角深色小浮窗：显示一句话，过一会儿自己淡出消失。

    用 ``Qt.Tool`` 顶层窗口（不是内嵌控件），所以不会挤动任何布局；
    鼠标事件穿透，浮在按钮上方也不会挡住点击。
    """

    _FADE_MS = 260

    def __init__(self, text: str, host: QWidget) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(host, flags)
        self.setObjectName("ZqapiToast")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 11, 20, 11)
        label = QLabel(str(text))
        label.setStyleSheet("color: #ffffff; font-size: 13px; font-weight: 700; background: transparent;")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

        self._fade: QPropertyAnimation | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start_fade)

    def paintEvent(self, event) -> None:  # noqa: D102 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1e293b"))
        painter.drawRoundedRect(self.rect(), 10, 10)
        painter.end()
        super().paintEvent(event)

    def _host_rect(self):
        host = self.parentWidget()
        if host is None:
            return None
        try:
            return host.frameGeometry()
        except RuntimeError:  # pragma: no cover - 宿主已销毁
            return None

    def popup(self, duration: int = 1500) -> None:
        self.adjustSize()
        rect = self._host_rect()
        if rect is not None:
            # 水平居中、贴着宿主窗口顶部往下一点，视线不用去找
            self.move(rect.center().x() - self.width() // 2, rect.top() + 96)
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self._timer.start(max(300, int(duration)))

    def _start_fade(self) -> None:
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(self._FADE_MS)
        self._fade.setStartValue(float(self.windowOpacity()))
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self.close)
        self._fade.start()


def show_toast(anchor: QWidget, text: str, *, duration: int = 1500) -> Optional["ToastMessage"]:
    """在 ``anchor`` 所属窗口里弹一条自动消失的提示（同一个窗口同时只留一条）。"""
    host = anchor.window() if anchor is not None else None
    if host is None:  # pragma: no cover - 控件未挂到窗口
        return None
    previous = getattr(host, "_zqapi_toast", None)
    if previous is not None:
        try:
            previous.close()
        except RuntimeError:  # pragma: no cover - 已经销毁
            pass
    toast = ToastMessage(str(text), host)
    setattr(host, "_zqapi_toast", toast)
    toast.popup(duration)
    return toast


# --------------------------------------------------------------------- 通用面板与控件


class CardPanel(QWidget):
    """带标题行的标准白色圆角卡片容器（复用于朱雀检测与文本比较面板）。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CardPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)

        self.layout_root = QVBoxLayout(self)
        self.layout_root.setContentsMargins(0, 0, 0, 0)
        self.layout_root.setSpacing(0)

        self.header = QWidget(self)
        self.header.setObjectName("CardPanelHeader")
        self.header_layout = QHBoxLayout(self.header)
        self.header_layout.setContentsMargins(12, 8, 10, 8)
        self.header_layout.setSpacing(8)

        self.title_label = QLabel(title, self.header)
        self.title_label.setObjectName("CardPanelTitle")
        self.hint_label = QLabel("", self.header)
        self.hint_label.setObjectName("CardPanelHint")

        self.header_layout.addWidget(self.title_label)
        self.header_layout.addWidget(self.hint_label)
        self.layout_root.addWidget(self.header)

    def set_hint(self, text: str) -> None:
        self.hint_label.setText(str(text or ""))

    def add_content(self, widget: QWidget, stretch: int = 1) -> None:
        self.layout_root.addWidget(widget, stretch)


def make_segmented_button(text: str, parent: QWidget | None = None) -> QToolButton:
    btn = QToolButton(parent)
    btn.setObjectName("ZqapiSegBtn")
    btn.setText(text)
    btn.setCheckable(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setMinimumHeight(26)
    return btn


# --------------------------------------------------------------------- 文本文件工具

TEXT_FILE_EXTENSIONS = (".txt", ".md", ".markdown", ".text")


def decode_text_bytes(raw: bytes) -> str:
    """自动探测编码将字节解码为文本，支持常见中英文编码。"""
    for encoding in ("utf-8-sig", "utf-8", "gbk", "big5", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_dropped_text_file(mime: Any) -> Optional[os.PathLike | str]:
    """从拖拽的 QMimeData 中提取首个合法本地文本文件路径。"""
    if mime is None or not hasattr(mime, "hasUrls") or not mime.hasUrls():
        return None
    from pathlib import Path

    for url in mime.urls():
        if not url.isLocalFile():
            continue
        p = Path(url.toLocalFile())
        if p.is_file() and p.suffix.lower() in TEXT_FILE_EXTENSIONS:
            return p
    return None


COMMON_SCROLLBAR_QSS = """
QScrollBar:vertical { border: none; background: transparent; width: 8px; margin: 0px; }
QScrollBar::handle:vertical { background: rgba(100, 116, 139, 0.25); border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: rgba(100, 116, 139, 0.45); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; background: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { border: none; background: transparent; height: 8px; margin: 0px; }
QScrollBar::handle:horizontal { background: rgba(100, 116, 139, 0.25); border-radius: 4px; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: rgba(100, 116, 139, 0.45); }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; background: none; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }
"""


# 全程序统一的下拉框样式。**所有** QComboBox 都套这一份，别再各写一套 ——
# 只要有人给某个下拉单独调了圆角/字号/箭头尺寸，两个下拉摆在一起就会明显不一样。
# 用法：COMBO_QSS.replace("__NAME__", "XxxCombo").replace("__ARROW__", arrow_url)
COMBO_QSS = """
QComboBox#__NAME__ {
    min-height: 32px; max-height: 34px; padding: 2px 28px 2px 10px; border-radius: 8px;
    border: 1px solid #e2e8f0; background: #ffffff; color: #111827; font-weight: 600;
}
QComboBox#__NAME__:hover { border-color: #cbd5e1; }
QComboBox#__NAME__::drop-down { border: none; width: 24px; background: transparent; }
QComboBox#__NAME__::down-arrow { image: url("__ARROW__"); width: 12px; height: 12px; margin-right: 6px; }
QComboBox#__NAME__ QAbstractItemView {
    background: #ffffff; border: 1px solid #dbe3ee; border-radius: 8px; padding: 5px;
    outline: none; selection-background-color: #f3f4f6; selection-color: #111827;
}
QComboBox#__NAME__ QAbstractItemView::item {
    min-height: 28px; padding: 4px 12px; border-radius: 6px; color: #111827;
}
QComboBox#__NAME__ QAbstractItemView::item:hover,
QComboBox#__NAME__ QAbstractItemView::item:selected { background-color: #f3f4f6; color: #111827; }
"""


def combo_qss(name: str) -> str:
    """按控件 objectName 生成统一的下拉框样式。"""
    return COMBO_QSS.replace("__NAME__", str(name)).replace("__ARROW__", asset_path("icon_combo_arrow.svg"))


def polish_combo_popup(combo: QComboBox) -> None:
    """让下拉弹窗圆角外的四个角不再发黑（老版本 Qt 的坑）。

    实测 Qt 6.10（系统 Python 那套）里 QComboBox 的弹窗容器是个**半透明窗口**，
    而 QSS 的圆角只画在里面那个 view 上 —— 圆角外那一圈没有任何东西覆盖，
    屏幕上合成出来就是四个黑角；Qt 6.11 起容器默认不透明，所以同一个程序
    换个解释器跑就"有的黑、有的不黑"。

    把容器改成"不透明 + 白底"有个前提：属性要在**原生窗口已建出来、但还没显示**的时候改。
    所以这里先 winId() 把原生窗口建出来，改完属性再 destroy() 一次，
    让它下次 show 时按新属性重建 —— 用户第一次点开下拉就是干净的。
    """
    view = combo.view()
    container = view.window() if view is not None else None
    if container is None or container is combo:
        return
    container.winId()  # 不先把窗口建出来，后面改属性不生效
    container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    container.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
    palette = container.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
    container.setPalette(palette)
    container.setAutoFillBackground(True)
    handle = container.windowHandle()
    if handle is not None:
        handle.destroy()  # 下次显示时按新属性重建原生窗口


def make_combo(parent: QWidget | None = None, name: str = "ZqapiCombo") -> QComboBox:
    """创建统一样式的下拉框。

    注意：圆角弹窗的"黑角"修补（polish_combo_popup）**不能放在这里** ——
    控件刚 new 出来时它的弹窗容器还没建好，修补会静默失效；
    等整个界面装好之后再统一调用 :func:`polish_combo_popups`。
    """
    combo = QComboBox(parent)
    combo.setObjectName(str(name))
    combo.setStyleSheet(combo_qss(name))
    return combo


def polish_combo_popups(root: QWidget) -> int:
    """把 root 里所有下拉框的弹窗都修一遍，返回处理个数。

    在界面装配完成、窗口显示之前调用（本项目放在 ToolboxWindow.__init__ 末尾）。
    """
    count = 0
    for combo in root.findChildren(QComboBox):
        view = combo.view()
        container = view.window() if view is not None else None
        if container is None or container is combo:
            continue
        polish_combo_popup(combo)
        count += 1
    return count
