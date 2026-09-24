"""朱雀检测-待检测文本编辑器子组件。

负责文本编辑、字数统计、拖拽/导入文件、分段底色高亮及段落微光定位动画。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QTextCharFormat,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QToolButton,
    QWidget,
)

from ui_kit import (
    CardPanel,
    decode_text_bytes,
    extract_dropped_text_file,
    load_icon,
)
import zhuque_text as zt


class ZhuqueEditorPanel(CardPanel):
    """待检测文本编辑面板。"""

    detect_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    clear_requested = pyqtSignal()
    text_changed = pyqtSignal(str)
    file_imported = pyqtSignal(str, int)  # filename, char_count
    status_hint = pyqtSignal(str, str)   # message, tone

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("待检测文本", parent)
        self._busy = False
        self._highlight_timer: Optional[QTimer] = None
        self._active_flash_ranges: list[tuple[int, int]] = []
        self._saved_report: Optional[zt.ZhuqueReport] = None

        self._build_header_actions()
        self._build_editor()

    def _build_header_actions(self) -> None:
        # 导入文件按钮
        self.import_btn = QToolButton(self.header)
        self.import_btn.setObjectName("ZhuqueNewButton")
        self.import_btn.setIcon(load_icon("icon_import_plus.svg"))
        self.import_btn.setIconSize(QSize(16, 16))
        self.import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_btn.setFixedSize(24, 24)
        self.import_btn.setToolTip("导入本地文本文件（.txt / .md），也可以把文件直接拖入文本框")
        self.import_btn.setAccessibleName("导入文件")
        self.import_btn.clicked.connect(self._on_import_clicked)

        # 清空按钮
        self.clear_btn = QPushButton("清空", self.header)
        self.clear_btn.setObjectName("ZhuqueHeaderBtn")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setMinimumHeight(26)
        self.clear_btn.setToolTip("清空输入框与当前结果（不影响历史记录）")
        self.clear_btn.clicked.connect(self._on_clear_clicked)

        # 检测/取消合一按钮
        self.detect_btn = QPushButton("检测", self.header)
        self.detect_btn.setObjectName("ZhuqueHeaderPrimary")
        self.detect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.detect_btn.setIcon(load_icon("icon_zhuque_white.svg"))
        self.detect_btn.setIconSize(QSize(15, 15))
        self.detect_btn.setMinimumHeight(26)
        self.detect_btn.setMinimumWidth(76)
        self.detect_btn.setToolTip("把输入框里的文本送朱雀检测（Ctrl+Enter）")
        self.detect_btn.clicked.connect(self._on_detect_or_cancel_clicked)

        self.header_layout.addWidget(self.import_btn)
        self.header_layout.addWidget(self.clear_btn)
        self.header_layout.addWidget(self.detect_btn)

    def _build_editor(self) -> None:
        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setObjectName("ZhuqueEditor")
        self.text_edit.setPlaceholderText("粘贴需要检测的文本…（Ctrl+Enter 直接检测，支持拖入 .txt / .md 文件）")
        self.text_edit.textChanged.connect(self._on_text_changed)
        self.text_edit.installEventFilter(self)
        self.add_content(self.text_edit, stretch=1)
        self._refresh_count_hint()

    def text(self) -> str:
        return self.text_edit.toPlainText()

    def set_text(self, content: str) -> None:
        self.text_edit.blockSignals(True)
        self.text_edit.setPlainText(content)
        self.text_edit.blockSignals(False)
        self._saved_report = None
        self.clear_highlights()
        self._refresh_count_hint()

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.detect_btn.setText("取消" if self._busy else "检测")
        self.detect_btn.setIcon(QIcon() if self._busy else load_icon("icon_zhuque_white.svg"))
        self.detect_btn.setToolTip(
            "中断当前请求（已经发出的请求将丢弃结果且不计入历史）"
            if self._busy
            else "把输入框里的文本送朱雀检测（Ctrl+Enter）"
        )

    def _on_detect_or_cancel_clicked(self) -> None:
        if self._busy:
            self.cancel_requested.emit()
        else:
            self.detect_requested.emit()

    def _on_clear_clicked(self) -> None:
        self.text_edit.clear()
        self._saved_report = None
        self.clear_highlights()
        self._refresh_count_hint()
        self.clear_requested.emit()

    def _on_text_changed(self) -> None:
        # 用户改动正文时清掉旧的分段底色
        self.clear_highlights()
        self._refresh_count_hint()
        self.text_changed.emit(self.text())

    def _refresh_count_hint(self) -> None:
        length = len(self.text())
        self.set_hint(f"{length} 字")

    def clear_highlights(self) -> None:
        self.text_edit.setExtraSelections([])

    def apply_highlights(self, report: zt.ZhuqueReport) -> None:
        """把逐段判定结果用底色标到输入框正文上。"""
        self._saved_report = report
        selections: list[QTextEdit.ExtraSelection] = []
        document = self.text_edit.document()
        total = len(self.text())
        if total <= 0:
            self.clear_highlights()
            return

        if report.segments:
            ranges = zt.segment_ranges(report.segments, total)
        else:
            start, end, _tint = zt.full_range_tint(report)
            ranges = [(start, end, None)]  # type: ignore[list-item]

        for start, end, segment in ranges:
            tint = zt.segment_tint(segment.label) if segment is not None else _tint
            cursor = QTextCursor(document)
            cursor.setPosition(max(0, start))
            cursor.setPosition(min(total, end), QTextCursor.MoveMode.KeepAnchor)
            char_format = QTextCharFormat()
            char_format.setBackground(QColor(tint))
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = char_format
            selections.append(sel)

        self.text_edit.setExtraSelections(selections)

    def jump_to_segment(self, order: int) -> None:
        """光标跳到指定段落并在该段落短暂呈现高亮微光。"""
        report = self._saved_report
        if report is None:
            return
        total = len(self.text())
        for start, end, seg in zt.segment_ranges(report.segments, total):
            if seg.order != order:
                continue
            document = self.text_edit.document()
            cursor = QTextCursor(document)
            cursor.setPosition(min(total, end))
            # 反向选区：光标停在段首，确保自动滚到可视区域段首
            cursor.setPosition(max(0, start), QTextCursor.MoveMode.KeepAnchor)
            self.text_edit.setTextCursor(cursor)
            self.text_edit.ensureCursorVisible()

            # 闪烁微光增强视觉反馈
            self._flash_segment(start, end)
            return

    def _flash_segment(self, start: int, end: int) -> None:
        """为定位段落叠加短暂的微光强调色。"""
        document = self.text_edit.document()
        total = len(self.text())
        flash_sel = QTextEdit.ExtraSelection()
        cursor = QTextCursor(document)
        cursor.setPosition(max(0, start))
        cursor.setPosition(min(total, end), QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#fef08a"))  # 醒目的淡黄微光
        flash_sel.cursor = cursor
        flash_sel.format = fmt

        current_sels = list(self.text_edit.extraSelections())
        current_sels.append(flash_sel)
        self.text_edit.setExtraSelections(current_sels)

        # 750ms 后恢复常态底色
        if self._highlight_timer is not None:
            self._highlight_timer.stop()
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self._restore_highlights)
        self._highlight_timer.start(750)

    def _restore_highlights(self) -> None:
        if self._saved_report is not None:
            self.apply_highlights(self._saved_report)

    # ------------------------------------------------------------------ 导入与拖拽
    def load_file(self, path: Path | str) -> bool:
        p = Path(path)
        try:
            raw = p.read_bytes()
        except OSError as exc:
            self.status_hint.emit(f"导入失败：{exc}", "error")
            return False
        content = decode_text_bytes(raw)
        self.set_text(content)
        self.text_edit.setFocus()
        self.file_imported.emit(p.name, len(content))
        return True

    def _on_import_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入文本文件",
            "",
            "文本文件 (*.txt *.md *.markdown *.text);;所有文件 (*)",
        )
        if path:
            self.load_file(path)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.text_edit:
            event_type = event.type()
            # 文件拖拽处理
            if event_type in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
                mime = getattr(event, "mimeData", lambda: None)()
                dropped = extract_dropped_text_file(mime)
                if dropped is not None:
                    if event_type == QEvent.Type.Drop:
                        self.load_file(dropped)
                        event.accept()
                        return True
                    event.setDropAction(Qt.DropAction.CopyAction)
                    event.accept()
                    return True

            # 快捷键处理
            if event_type == QEvent.Type.KeyPress:
                key = getattr(event, "key", lambda: None)()
                modifiers = getattr(event, "modifiers", lambda: Qt.KeyboardModifier.NoModifier)()
                if (modifiers & Qt.KeyboardModifier.ControlModifier) and key in (
                    Qt.Key.Key_Return,
                    Qt.Key.Key_Enter,
                ):
                    if not self._busy:
                        self.detect_requested.emit()
                    return True

        return super().eventFilter(obj, event)
