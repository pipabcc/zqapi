"""文本比较标签页：逐处标记改写文相对原文的新增、删除与修改。

功能增强：
- 支持原文与对比结果的双栏等比同步滚动
- 原文与改写文分别支持加号导入与直接拖拽文本文件（.txt / .md）
- 选项状态（忽略空白差异、忽略大小写、同步滚动）持久化记忆
- 后台轻量比对，防止卡顿
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QEvent, QMimeData, QObject, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import text_diff
from ui_kit import (
    COMMON_SCROLLBAR_QSS,
    asset_path,
    decode_text_bytes,
    extract_dropped_text_file,
    install_custom_text_context_menus,
    load_icon,
    make_segmented_button,
)
from zhuque_store import ZhuqueStore


_EDITOR_VIEW = 0
_RESULT_VIEW = 1

_STATUS_TONES = {
    "info": "#64748b",
    "success": "#15803d",
    "warning": "#b45309",
    "error": "#b91c1c",
}

_RUNNING_WORKERS: set["_TextCompareWorker"] = set()
_BUSY_UI_DELAY_MS = 200


def _chip(text: str, *, color: str, background: str, strike: bool = False) -> str:
    decoration = "text-decoration:line-through;" if strike else ""
    return (
        f'<span style="color:{color}; background-color:{background}; {decoration}'
        f'padding:1px 4px; border-radius:3px;">{text}</span>'
    )


def _legend_html() -> str:
    return (
        "图例："
        + _chip("新增", color=text_diff.INSERT_COLOR, background=text_diff.INSERT_BACKGROUND)
        + " "
        + _chip("删除", color=text_diff.DELETE_COLOR, background=text_diff.DELETE_BACKGROUND, strike=True)
        + " "
        + _chip("修改", color=text_diff.DELETE_COLOR, background=text_diff.DELETE_BACKGROUND, strike=True)
        + '<span style="color:#94a3b8;">→</span>'
        + _chip("新内容", color=text_diff.INSERT_COLOR, background=text_diff.INSERT_BACKGROUND)
    )


def _empty_result_html() -> str:
    return (
        '<div style="color:#94a3b8; font-size:13px; line-height:190%;">'
        "点击「开始比较」后，这里会以富文本逐处标记改写文相对原文的改动：<br/>"
        + _chip("新增内容", color=text_diff.INSERT_COLOR, background=text_diff.INSERT_BACKGROUND)
        + "&nbsp;"
        + _chip(
            "删除内容",
            color=text_diff.DELETE_COLOR,
            background=text_diff.DELETE_BACKGROUND,
            strike=True,
        )
        + "&nbsp;其余未改动内容保持原样。</div>"
    )


class _TextCompareWorker(QThread):
    """在后台线程里执行差异比对，返回 :class:`text_diff.DiffResult`。"""

    finished_ok = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)

    def __init__(
        self,
        request_id: int,
        original: str,
        rewritten: str,
        *,
        ignore_whitespace: bool,
        ignore_case: bool,
    ) -> None:
        super().__init__()
        self._request_id = int(request_id)
        self._original = str(original or "")
        self._rewritten = str(rewritten or "")
        self._ignore_whitespace = bool(ignore_whitespace)
        self._ignore_case = bool(ignore_case)
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            if self._cancelled:
                return
            result = text_diff.compare_texts(
                self._original,
                self._rewritten,
                ignore_whitespace=self._ignore_whitespace,
                ignore_case=self._ignore_case,
            )
            if self._cancelled:
                return
        except Exception as exc:
            self.failed.emit(self._request_id, str(exc) or "比较失败。")
            return
        self.finished_ok.emit(self._request_id, result)


class TextCompareTab(QWidget):
    """「文本比较」标签页。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = ZhuqueStore()
        self._worker: Optional[_TextCompareWorker] = None
        self._request_seq = 0
        self._result: Optional[text_diff.DiffResult] = None
        self._stale = False
        self._busy = False
        self._pending_recompare = False
        self._status_text = ""
        self._syncing_scroll = False
        self._original_edit: Optional[QPlainTextEdit] = None
        self._rewritten_edit: Optional[QPlainTextEdit] = None

        self.setObjectName("TextComparePage")
        self._build_ui()
        self._apply_style()
        self._load_saved_options()
        self._setup_sync_scroll()
        install_custom_text_context_menus(self, self)

        # 延后上屏的忙碌态
        self._busy_timer = QTimer(self)
        self._busy_timer.setSingleShot(True)
        self._busy_timer.setInterval(_BUSY_UI_DELAY_MS)
        self._busy_timer.timeout.connect(self._show_busy_ui)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 14)
        root.setSpacing(10)
        root.addWidget(self._build_header())
        root.addWidget(self._build_panels(), 1)
        root.addWidget(self._build_status_row())

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        icon_label = QLabel()
        icon_label.setFixedSize(24, 24)
        icon_label.setPixmap(load_icon("icon_compare.svg").pixmap(QSize(24, 24)))

        subtitle = QLabel("逐处标记改写文相对原文的新增、删除与修改，支持双栏同步滚动与文件拖入")
        subtitle.setObjectName("TextCompareSubtitle")

        layout.addWidget(icon_label)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        return header

    def _build_compare_actions(self) -> list[QWidget]:
        self._swap_btn = QPushButton("交换")
        self._swap_btn.setObjectName("TextCompareSecondary")
        self._swap_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._swap_btn.setMinimumHeight(26)
        self._swap_btn.setToolTip("把原文与改写文互换后重新比较")
        self._swap_btn.clicked.connect(self._on_swap_clicked)

        self._clear_btn = QPushButton("清空")
        self._clear_btn.setObjectName("TextCompareSecondary")
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.setMinimumHeight(26)
        self._clear_btn.setToolTip("清空两段文本与对比结果")
        self._clear_btn.clicked.connect(self._on_clear_clicked)

        self._compare_btn = QPushButton("开始比较")
        self._compare_btn.setObjectName("TextComparePrimary")
        self._compare_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._compare_btn.setIcon(load_icon("icon_compare_white.svg"))
        self._compare_btn.setIconSize(QSize(16, 16))
        self._compare_btn.setMinimumHeight(26)
        self._compare_btn.setMinimumWidth(96)
        self._compare_btn.setToolTip("比较两段文本的差异（Ctrl+Enter）")
        self._compare_btn.clicked.connect(self._on_compare_clicked)

        return [self._swap_btn, self._clear_btn, self._compare_btn]

    def _build_compare_options(self) -> list[QWidget]:
        self._ignore_whitespace_check = QCheckBox("忽略空白差异")
        self._ignore_whitespace_check.setObjectName("TextCompareOption")
        self._ignore_whitespace_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ignore_whitespace_check.setToolTip(
            "连续空白视为一个空格、去掉首尾空白后再比较；结果区展示的也是规范化后的文本。"
        )

        self._ignore_case_check = QCheckBox("忽略英文大小写")
        self._ignore_case_check.setObjectName("TextCompareOption")
        self._ignore_case_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ignore_case_check.setToolTip("英文大小写差异不计入改动，展示仍保留原始大小写。")

        self._sync_scroll_check = QCheckBox("同步滚动")
        self._sync_scroll_check.setObjectName("TextCompareOption")
        self._sync_scroll_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sync_scroll_check.setToolTip("开启后，原文与改写文/结果区将等比联动同步滚动。")

        for check in (self._ignore_whitespace_check, self._ignore_case_check, self._sync_scroll_check):
            check.toggled.connect(self._on_option_toggled)

        return [self._ignore_whitespace_check, self._ignore_case_check, self._sync_scroll_check]

    def _build_panels(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("TextCompareSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)
        splitter.addWidget(self._build_original_panel())
        splitter.addWidget(self._build_rewritten_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([560, 560])
        self._splitter = splitter
        return splitter

    def _make_panel(self, title: str) -> tuple[QWidget, QLabel, QHBoxLayout]:
        panel = QWidget()
        panel.setObjectName("TextComparePanel")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header.setObjectName("TextComparePanelHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 10, 8)
        header_layout.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("TextComparePanelTitle")
        counter = QLabel("0 字")
        counter.setObjectName("TextCompareCounter")
        header_layout.addWidget(label)
        header_layout.addWidget(counter)
        layout.addWidget(header)
        return panel, counter, header_layout

    def _build_original_panel(self) -> QWidget:
        panel, counter, header_layout = self._make_panel("原文")
        self._original_counter = counter
        self._original_panel = panel

        # 加号导入按钮
        self._orig_import_btn = QToolButton()
        self._orig_import_btn.setObjectName("TextCompareImportBtn")
        self._orig_import_btn.setIcon(load_icon("icon_import_plus.svg"))
        self._orig_import_btn.setIconSize(QSize(16, 16))
        self._orig_import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._orig_import_btn.setFixedSize(24, 24)
        self._orig_import_btn.setToolTip("导入原文文件（.txt / .md），或直接拖入下方文本框")
        self._orig_import_btn.clicked.connect(lambda: self._on_import_file_clicked(self._original_edit))

        header_layout.addWidget(self._orig_import_btn)
        header_layout.addStretch(1)
        for button in self._build_compare_actions():
            header_layout.addWidget(button)

        self._original_edit = QPlainTextEdit()
        self._original_edit.setObjectName("TextCompareEditor")
        self._original_edit.setPlaceholderText("粘贴作为基准的原文…（支持拖入 .txt / .md 文件）")
        self._original_edit.textChanged.connect(self._on_text_changed)
        self._original_edit.installEventFilter(self)
        layout: QVBoxLayout = panel.layout()  # type: ignore[assignment]
        layout.addWidget(self._original_edit, 1)
        return panel

    def _build_rewritten_panel(self) -> QWidget:
        panel, counter, header_layout = self._make_panel("改写文")
        self._rewritten_counter = counter
        self._rewritten_panel = panel

        # 加号导入按钮
        self._rewritten_import_btn = QToolButton()
        self._rewritten_import_btn.setObjectName("TextCompareImportBtn")
        self._rewritten_import_btn.setIcon(load_icon("icon_import_plus.svg"))
        self._rewritten_import_btn.setIconSize(QSize(16, 16))
        self._rewritten_import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._rewritten_import_btn.setFixedSize(24, 24)
        self._rewritten_import_btn.setToolTip("导入改写文文件（.txt / .md），或直接拖入下方文本框")
        self._rewritten_import_btn.clicked.connect(lambda: self._on_import_file_clicked(self._rewritten_edit))

        header_layout.addWidget(self._rewritten_import_btn)

        # 选项
        for check in self._build_compare_options():
            header_layout.addWidget(check)
        header_layout.addStretch(1)

        self._input_tab_btn = make_segmented_button("输入")
        self._result_tab_btn = make_segmented_button("对比结果")
        self._input_tab_btn.setChecked(True)
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self._input_tab_btn, _EDITOR_VIEW)
        group.addButton(self._result_tab_btn, _RESULT_VIEW)
        group.idClicked.connect(self._on_view_switched)
        self._view_group = group

        self._copy_btn = QPushButton("复制结果")
        self._copy_btn.setObjectName("TextCompareSecondary")
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.setEnabled(False)
        self._copy_btn.setMinimumHeight(26)
        self._copy_btn.setToolTip("复制带颜色标记的差异结果（粘贴到 Word / 聊天窗口仍保留颜色）")
        self._copy_btn.clicked.connect(self._on_copy_result)

        header_layout.addWidget(self._input_tab_btn)
        header_layout.addWidget(self._result_tab_btn)
        header_layout.addWidget(self._copy_btn)

        self._rewritten_edit = QPlainTextEdit()
        self._rewritten_edit.setObjectName("TextCompareEditor")
        self._rewritten_edit.setPlaceholderText("粘贴改写后的文本…（支持拖入 .txt / .md 文件）")
        self._rewritten_edit.textChanged.connect(self._on_text_changed)
        self._rewritten_edit.installEventFilter(self)

        self._result_view = QTextBrowser()
        self._result_view.setObjectName("TextCompareResult")
        self._result_view.setReadOnly(True)
        self._result_view.setOpenExternalLinks(False)
        self._result_view.setOpenLinks(False)
        self._result_view.setHtml(_empty_result_html())

        self._rewritten_stack = QStackedWidget()
        self._rewritten_stack.addWidget(self._rewritten_edit)
        self._rewritten_stack.addWidget(self._result_view)
        self._rewritten_stack.setCurrentIndex(_EDITOR_VIEW)

        layout: QVBoxLayout = panel.layout()  # type: ignore[assignment]
        layout.addWidget(self._rewritten_stack, 1)
        return panel

    def _build_status_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        legend = QLabel(_legend_html())
        legend.setObjectName("TextCompareLegend")
        legend.setTextFormat(Qt.TextFormat.RichText)

        self._status_label = QLabel("")
        self._status_label.setObjectName("TextCompareStatus")

        self._stats_label = QLabel("")
        self._stats_label.setObjectName("TextCompareStats")
        self._stats_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(legend)
        layout.addWidget(self._status_label)
        layout.addStretch(1)
        layout.addWidget(self._stats_label)
        self._set_status("就绪：粘贴原文与改写文后点击「开始比较」。", tone="info")
        return row

    def _apply_style(self) -> None:
        checkbox_icon_url = asset_path("icon_checkbox_check_slate.svg")
        self.setStyleSheet(
            f"""
            QWidget#TextComparePage {{ background: transparent; }}
            QWidget {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 13px; color: #111827; }}
            QLabel#TextCompareSubtitle {{ color: #64748b; font-size: 12px; font-weight: 600; }}
            QWidget#TextComparePanel {{ background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px; }}
            QWidget#TextComparePanelHeader {{ background: transparent; border: none; }}
            QLabel#TextComparePanelTitle {{ font-size: 13px; font-weight: 800; color: #334155; }}
            QLabel#TextCompareCounter {{ color: #94a3b8; font-size: 11px; font-weight: 600; }}
            QToolButton#TextCompareImportBtn {{
                background: #f1f5f9; color: #475569; border: none; border-radius: 12px; padding: 0px;
            }}
            QToolButton#TextCompareImportBtn:hover {{ background: #e2e8f0; color: #0f172a; }}
            QPlainTextEdit#TextCompareEditor {{
                background: transparent; border: none; padding: 4px 10px 10px 10px; font-size: 13px; color: #111827;
            }}
            QTextBrowser#TextCompareResult {{ background: transparent; border: none; padding: 4px 10px 10px 10px; }}
            QToolButton#ZqapiSegBtn {{
                background: transparent; border: none; border-radius: 6px; padding: 3px 12px;
                color: #64748b; font-weight: 700;
            }}
            QToolButton#ZqapiSegBtn:hover {{ background: #eef2f7; color: #334155; }}
            QToolButton#ZqapiSegBtn:checked {{ background: #1e293b; color: #ffffff; }}
            QPushButton#TextComparePrimary {{
                background: #1e293b; color: #ffffff; border: none; border-radius: 8px;
                padding: 0 14px; font-size: 13px; font-weight: 800;
            }}
            QPushButton#TextComparePrimary:hover {{ background: #334155; }}
            QPushButton#TextComparePrimary:pressed {{ background: #0f172a; }}
            QPushButton#TextComparePrimary:disabled {{ background: #cbd5e1; color: #f8fafc; }}
            QPushButton#TextCompareSecondary {{
                background: #f1f5f9; color: #334155; border: none; border-radius: 8px;
                padding: 0 12px; font-size: 12px; font-weight: 700;
            }}
            QPushButton#TextCompareSecondary:hover {{ background: #e2e8f0; color: #0f172a; }}
            QPushButton#TextCompareSecondary:disabled {{ background: #f8fafc; color: #cbd5e1; }}
            QCheckBox#TextCompareOption {{ background: transparent; color: #64748b; font-size: 12px; font-weight: 600; padding: 0 2px; }}
            QCheckBox#TextCompareOption::indicator {{
                width: 14px; height: 14px; border-radius: 4px; border: 1px solid #94a3b8; background: transparent;
            }}
            QCheckBox#TextCompareOption::indicator:hover {{ border-color: #64748b; background: rgba(241, 245, 249, 0.62); }}
            QCheckBox#TextCompareOption::indicator:checked {{
                border-color: #64748b; background: transparent; image: url('{checkbox_icon_url}');
            }}
            QLabel#TextCompareLegend {{ color: #64748b; font-size: 12px; font-weight: 600; }}
            QLabel#TextCompareStatus {{ font-size: 12px; font-weight: 600; }}
            QLabel#TextCompareStats {{ color: #475569; font-size: 12px; font-weight: 700; }}
            QSplitter#TextCompareSplitter::handle {{ background: transparent; }}
            {COMMON_SCROLLBAR_QSS}
            """
        )

    # --- 配置持久化 -------------------------------------------------------

    def _load_saved_options(self) -> None:
        self._ignore_whitespace_check.blockSignals(True)
        self._ignore_case_check.blockSignals(True)
        self._sync_scroll_check.blockSignals(True)

        self._ignore_whitespace_check.setChecked(
            bool(self._store.get_custom_setting("compare_ignore_whitespace", False))
        )
        self._ignore_case_check.setChecked(
            bool(self._store.get_custom_setting("compare_ignore_case", False))
        )
        self._sync_scroll_check.setChecked(
            bool(self._store.get_custom_setting("compare_sync_scroll", True))
        )

        self._ignore_whitespace_check.blockSignals(False)
        self._ignore_case_check.blockSignals(False)
        self._sync_scroll_check.blockSignals(False)

    def _save_options(self) -> None:
        self._store.set_custom_setting(
            "compare_ignore_whitespace", self._ignore_whitespace_check.isChecked()
        )
        self._store.set_custom_setting("compare_ignore_case", self._ignore_case_check.isChecked())
        self._store.set_custom_setting("compare_sync_scroll", self._sync_scroll_check.isChecked())

    # --- 同步滚动联动 -----------------------------------------------------

    def _setup_sync_scroll(self) -> None:
        orig_bar = self._original_edit.verticalScrollBar()
        res_bar = self._result_view.verticalScrollBar()
        rewr_bar = self._rewritten_edit.verticalScrollBar()

        orig_bar.valueChanged.connect(lambda v: self._sync_scroll_from(self._original_edit, v))
        res_bar.valueChanged.connect(lambda v: self._sync_scroll_from(self._result_view, v))
        rewr_bar.valueChanged.connect(lambda v: self._sync_scroll_from(self._rewritten_edit, v))

    def _sync_scroll_from(self, source_widget: QWidget, value: int) -> None:
        if not self._sync_scroll_check.isChecked() or self._syncing_scroll:
            return

        source_bar = getattr(source_widget, "verticalScrollBar", lambda: None)()
        if source_bar is None or source_bar.maximum() <= 0:
            return

        ratio = float(value) / float(source_bar.maximum())
        self._syncing_scroll = True
        try:
            if source_widget is self._original_edit:
                # 原文滚 -> 同步右侧当前可见视图
                target = (
                    self._result_view
                    if self._rewritten_stack.currentIndex() == _RESULT_VIEW
                    else self._rewritten_edit
                )
                bar = target.verticalScrollBar()
                if bar.maximum() > 0:
                    bar.setValue(int(round(ratio * bar.maximum())))
            else:
                # 右侧滚 -> 同步原文
                bar = self._original_edit.verticalScrollBar()
                if bar.maximum() > 0:
                    bar.setValue(int(round(ratio * bar.maximum())))
        finally:
            self._syncing_scroll = False

    # --- 交互 -------------------------------------------------------------

    def _set_status(self, text: str, *, tone: str = "info") -> None:
        self._status_text = str(text)
        self._status_label.setText(self._status_text)
        self._status_label.setStyleSheet(f"color: {_STATUS_TONES.get(tone, _STATUS_TONES['info'])};")

    def _refresh_counters(self) -> None:
        self._original_counter.setText(f"{len(self._original_edit.toPlainText())} 字")
        self._rewritten_counter.setText(f"{len(self._rewritten_edit.toPlainText())} 字")

    def _on_text_changed(self) -> None:
        self._refresh_counters()
        if self._result is None or self._busy or self._stale:
            return
        self._stale = True
        self._set_status("内容已改动，点击「开始比较」刷新对比结果。", tone="warning")

    def _on_option_toggled(self, _checked: bool) -> None:
        self._save_options()
        if self._result is None:
            return
        if self._busy:
            self._pending_recompare = True
            return
        self._start_compare()

    def _on_view_switched(self, view_id: int) -> None:
        self._rewritten_stack.setCurrentIndex(int(view_id))

    def _show_result_view(self) -> None:
        self._result_tab_btn.setChecked(True)
        self._rewritten_stack.setCurrentIndex(_RESULT_VIEW)

    def _on_compare_clicked(self) -> None:
        self._start_compare()

    def _start_compare(self) -> None:
        original = self._original_edit.toPlainText()
        rewritten = self._rewritten_edit.toPlainText()
        if not original.strip() and not rewritten.strip():
            self._set_status("请先在「原文」与「改写文」中输入或粘贴内容。", tone="warning")
            return
        if self._worker is not None and self._worker.isRunning():
            return

        self._request_seq += 1
        self._busy = True
        self._busy_timer.start()

        worker = _TextCompareWorker(
            self._request_seq,
            original,
            rewritten,
            ignore_whitespace=self._ignore_whitespace_check.isChecked(),
            ignore_case=self._ignore_case_check.isChecked(),
        )
        self._worker = worker
        _RUNNING_WORKERS.add(worker)
        worker.finished_ok.connect(self._on_compare_finished)
        worker.failed.connect(self._on_compare_failed)
        worker.finished.connect(lambda w=worker: _RUNNING_WORKERS.discard(w))
        worker.start()

    def _show_busy_ui(self) -> None:
        if not self._busy:
            return
        try:
            self._compare_btn.setEnabled(False)
        except RuntimeError:
            return
        self._set_status("正在比较…", tone="info")

    def _finish_busy(self) -> None:
        self._busy = False
        self._busy_timer.stop()
        try:
            self._compare_btn.setEnabled(True)
        except RuntimeError:
            pass
        if self._pending_recompare:
            self._pending_recompare = False
            self._start_compare()

    def _on_compare_finished(self, request_id: int, result: object) -> None:
        if int(request_id) != self._request_seq:
            return
        self._finish_busy()
        if int(request_id) != self._request_seq:
            return
        if not isinstance(result, text_diff.DiffResult):
            self._on_compare_failed(request_id, "比较结果无效。")
            return
        self._result = result
        self._stale = False
        self._result_view.setHtml(text_diff.diff_to_html(result))
        self._copy_btn.setEnabled(True)
        self._show_result_view()
        self._stats_label.setText(text_diff.format_stats(result.stats))
        if result.stats.is_identical:
            self._set_status("比较完成：两段文本完全一致，未检测到差异。", tone="info")
            return
        self._set_status("比较完成，结果已在右侧「对比结果」中以颜色逐处标记。", tone="success")

    def _on_compare_failed(self, request_id: int, message: str) -> None:
        if int(request_id) != self._request_seq:
            return
        self._finish_busy()
        self._set_status(f"比较失败：{message}", tone="error")

    def _on_swap_clicked(self) -> None:
        original = self._original_edit.toPlainText()
        rewritten = self._rewritten_edit.toPlainText()
        if not original and not rewritten:
            self._set_status("两段文本都是空的，无需交换。", tone="warning")
            return
        self._original_edit.blockSignals(True)
        self._rewritten_edit.blockSignals(True)
        try:
            self._original_edit.setPlainText(rewritten)
            self._rewritten_edit.setPlainText(original)
        finally:
            self._original_edit.blockSignals(False)
            self._rewritten_edit.blockSignals(False)
        self._refresh_counters()
        if self._result is None:
            self._set_status("已交换原文与改写文。", tone="info")
            return
        self._start_compare()

    def _on_clear_clicked(self) -> None:
        for edit in (self._original_edit, self._rewritten_edit):
            edit.blockSignals(True)
        try:
            self._original_edit.clear()
            self._rewritten_edit.clear()
        finally:
            for edit in (self._original_edit, self._rewritten_edit):
                edit.blockSignals(False)
        self._refresh_counters()
        self._result = None
        self._stale = False
        self._request_seq += 1
        self._copy_btn.setEnabled(False)
        self._stats_label.setText("")
        self._result_view.setHtml(_empty_result_html())
        self._input_tab_btn.setChecked(True)
        self._rewritten_stack.setCurrentIndex(_EDITOR_VIEW)
        self._set_status("已清空，可以重新粘贴内容。", tone="info")
        self._original_edit.setFocus()

    def _on_copy_result(self) -> None:
        result = self._result
        if result is None:
            self._set_status("还没有可复制的对比结果。", tone="warning")
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            self._set_status("当前环境不支持剪贴板。", tone="error")
            return
        mime = QMimeData()
        mime.setHtml(text_diff.diff_to_html(result))
        mime.setText(text_diff.diff_to_marked_text(result))
        try:
            clipboard.setMimeData(mime)
        except Exception as exc:
            self._set_status(f"复制失败：{exc}", tone="error")
            return
        self._set_status("已复制差异结果：新增记为【+…+】、删除记为【-…-】。", tone="success")

    # --- 文件导入与拖拽 ---------------------------------------------------

    def _load_file_into(self, target_edit: QPlainTextEdit, file_path: Path | str) -> bool:
        p = Path(file_path)
        try:
            raw = p.read_bytes()
        except OSError as exc:
            self._set_status(f"导入失败：{exc}", tone="error")
            return False
        content = decode_text_bytes(raw)
        target_edit.setPlainText(content)
        target_edit.setFocus()
        self._set_status(f"已载入 {p.name}（{len(content)} 字）。", tone="success")
        return True

    def _on_import_file_clicked(self, target_edit: QPlainTextEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入文本文件",
            "",
            "文本文件 (*.txt *.md *.markdown *.text);;所有文件 (*)",
        )
        if path:
            self._load_file_into(target_edit, path)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        orig = getattr(self, "_original_edit", None)
        rewr = getattr(self, "_rewritten_edit", None)
        if obj in (orig, rewr) and obj is not None:
            event_type = event.type()
            # 文件拖拽处理
            if event_type in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
                mime = getattr(event, "mimeData", lambda: None)()
                dropped = extract_dropped_text_file(mime)
                if dropped is not None:
                    if event_type == QEvent.Type.Drop:
                        self._load_file_into(obj, dropped)  # type: ignore[arg-type]
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
                    self._start_compare()
                    return True

        return super().eventFilter(obj, event)

    # --- 页面生命周期 -----------------------------------------------------

    def on_shown(self) -> None:
        self._refresh_counters()

    def cleanup(self) -> None:
        worker = self._worker
        self._worker = None
        self._request_seq += 1
        if worker is not None:
            worker.cancel()
            for sig in (worker.finished_ok, worker.failed):
                try:
                    sig.disconnect(self)
                except Exception:
                    pass
            worker.wait(200)
