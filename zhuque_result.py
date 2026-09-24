"""朱雀检测-检测结果展示子组件。

负责渲染富文本报告、SVG 位图转换与注入、原始 JSON 高亮展示、视图切换、复制与导出。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from PyQt6.QtCore import QMimeData, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QPainter,
    QPixmap,
    QTextDocument,
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QWidget,
)

from ui_kit import (
    CardPanel,
    make_combo,
    make_segmented_button,
    show_toast,
)
import zhuque_text as zt


_RESULT_VIEW = 0
_RAW_VIEW = 1
_CHART_RESOURCE_NAME = "zhuque-chart.png"
_MAX_CHART_SCALE = 3.0


def _svg_pixmap(svg: str, width: int, dpr: float) -> Optional[QPixmap]:
    try:
        renderer = QSvgRenderer(str(svg or "").encode("utf-8"))
    except Exception:
        return None
    if not renderer.isValid():
        return None
    natural = renderer.defaultSize()
    if natural.width() <= 0 or natural.height() <= 0:
        return None
    width = max(1, int(width))
    height = max(1, int(round(natural.height() * width / float(natural.width()))))
    try:
        ratio = float(dpr or 1.0)
    except (TypeError, ValueError):
        ratio = 1.0
    if ratio <= 0:
        ratio = 1.0
    pixmap = QPixmap(max(1, int(round(width * ratio))), max(1, int(round(height * ratio))))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    return pixmap


class ZhuqueResultPanel(CardPanel):
    """检测结果展示面板。"""

    segment_anchor_clicked = pyqtSignal(int)
    options_changed = pyqtSignal()
    status_hint = pyqtSignal(str, str)  # msg, tone

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("检测结果", parent)
        self._report: Optional[zt.ZhuqueReport] = None
        self._report_mode: str = zt.DEFAULT_CHART_MODE
        self._chart_width = zt.CHART_WIDTH
        self._chart_dpr = 1.0

        self._build_header_controls()
        self._build_result_views()

        self._chart_timer = QTimer(self)
        self._chart_timer.setSingleShot(True)
        self._chart_timer.setInterval(0)
        self._chart_timer.timeout.connect(self._sync_chart_render)

    def _build_header_controls(self) -> None:
        # 1. 合并段落与图表选项
        options_box = QWidget(self.header)
        opt_layout = QHBoxLayout(options_box)
        opt_layout.setContentsMargins(0, 0, 0, 0)
        opt_layout.setSpacing(6)

        lbl_merge = QLabel("合并段落", options_box)
        lbl_merge.setObjectName("ZhuqueFieldLabel")
        self.merge_combo = make_combo(options_box, "ZhuqueCombo")
        self.merge_combo.setView(QListView())
        for val, name in zt.MERGE_MODES:
            self.merge_combo.addItem(name, val)
        self.merge_combo.setToolTip("is_merge：false = 逐段检测（默认），true = 整篇检测")
        self.merge_combo.currentIndexChanged.connect(lambda *_: self.options_changed.emit())

        lbl_chart = QLabel("图表", options_box)
        lbl_chart.setObjectName("ZhuqueFieldLabel")
        self.chart_combo = make_combo(options_box, "ZhuqueCombo")
        self.chart_combo.setView(QListView())
        for val, name in zt.chart_options():
            self.chart_combo.addItem(name, val)
        self.chart_combo.setToolTip("报告里的图表形式：SVG 图形 / 字符图 / 关闭")
        self.chart_combo.currentIndexChanged.connect(lambda *_: self.options_changed.emit())

        opt_layout.addWidget(lbl_merge)
        opt_layout.addWidget(self.merge_combo)
        opt_layout.addWidget(lbl_chart)
        opt_layout.addWidget(self.chart_combo)
        self.header_layout.addWidget(options_box)

        # 2. 视图切换按钮
        self.result_tab_btn = make_segmented_button("可视图表", self.header)
        self.raw_tab_btn = make_segmented_button("原始返回", self.header)
        self.result_tab_btn.setChecked(True)

        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.view_group.addButton(self.result_tab_btn, _RESULT_VIEW)
        self.view_group.addButton(self.raw_tab_btn, _RAW_VIEW)
        self.view_group.idClicked.connect(self._on_view_switched)

        self.header_layout.addWidget(self.result_tab_btn)
        self.header_layout.addWidget(self.raw_tab_btn)

        # 3. 复制与导出按钮
        self.copy_btn = QPushButton("复制结果", self.header)
        self.copy_btn.setObjectName("ZhuqueMini")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setToolTip("复制检测结论（粘贴到 Word / 聊天窗口仍保留颜色）")
        self.copy_btn.clicked.connect(self._on_copy_clicked)

        self.export_btn = QPushButton("导出", self.header)
        self.export_btn.setObjectName("ZhuqueMini")
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.setToolTip("把结论导出成 HTML 或 Markdown 文件（默认 HTML）")
        self.export_btn.clicked.connect(self._on_export_clicked)

        self.header_layout.addWidget(self.copy_btn)
        self.header_layout.addWidget(self.export_btn)
        self.set_actions_enabled(False)

    def _build_result_views(self) -> None:
        self.result_view = QTextBrowser(self)
        self.result_view.setObjectName("ZhuqueResult")
        self.result_view.setReadOnly(True)
        self.result_view.setOpenExternalLinks(False)
        self.result_view.setOpenLinks(False)
        self.result_view.anchorClicked.connect(self._on_anchor_clicked)
        self.result_view.setHtml(self._placeholder_html())

        self.raw_view = QTextBrowser(self)
        self.raw_view.setObjectName("ZhuqueResult")
        self.raw_view.setReadOnly(True)
        self.raw_view.setOpenExternalLinks(False)
        self.raw_view.setOpenLinks(False)
        self.raw_view.setHtml(self._empty_raw_html())

        self.result_stack = QStackedWidget(self)
        self.result_stack.addWidget(self.result_view)
        self.result_stack.addWidget(self.raw_view)
        self.result_stack.setCurrentIndex(_RESULT_VIEW)
        self.add_content(self.result_stack, stretch=1)

    def _placeholder_html(self) -> str:
        return (
            '<div style="color:#94a3b8; font-size:13px; line-height:190%;">'
            "在左侧粘贴文本并点击「检测」后，这里会显示朱雀的判定结果：<br/>"
            "• 文本长度、整体疑似 AI 内容占比、逐段判定构成<br/>"
            "• 逐段明细（含图形化图表）与整体判定<br/>"
            "• 原始返回可切到「原始返回」查看完整 JSON</div>"
        )

    def _empty_raw_html(self) -> str:
        return (
            '<div style="color:#94a3b8; font-size:13px; line-height:190%;">'
            "还没有原始返回。检测完成后，这里会显示上游接口返回的完整 JSON（带语法高亮）。</div>"
        )

    def set_actions_enabled(self, enabled: bool) -> None:
        self.copy_btn.setEnabled(bool(enabled))
        self.export_btn.setEnabled(bool(enabled))

    def _on_view_switched(self, view_id: int) -> None:
        self.result_stack.setCurrentIndex(int(view_id))

    def _on_anchor_clicked(self, url: QUrl) -> None:
        if str(url.scheme()).lower() != "seg":
            return
        raw = str(url.path()).strip("/")
        try:
            order = int(raw)
            self.segment_anchor_clicked.emit(order)
        except (TypeError, ValueError):
            pass

    def preferred_chart_width(self) -> int:
        viewport = max(200, int(self.result_view.viewport().width()) - 28)
        return max(320, min(zt.CHART_WIDTH, viewport))

    def current_dpr(self) -> float:
        try:
            ratio = float(self.devicePixelRatioF())
        except (TypeError, ValueError):
            ratio = 1.0
        if ratio <= 0:
            return 1.0
        return min(ratio, _MAX_CHART_SCALE)

    def clear(self) -> None:
        self._report = None
        self.set_actions_enabled(False)
        self.result_view.setHtml(self._placeholder_html())
        self.raw_view.setHtml(self._empty_raw_html())
        self.result_stack.setCurrentIndex(_RESULT_VIEW)
        self.result_tab_btn.setChecked(True)

    def display_failure(self, message: str) -> None:
        self._report = None
        self.set_actions_enabled(False)
        self.result_view.setHtml(
            '<div style="font-size:14px; font-weight:800; color:#b91c1c;">检测失败</div>'
            f'<div style="margin-top:6px; color:#475569; font-size:13px; line-height:180%;">{zt._esc(message)}</div>'
        )
        self.result_tab_btn.setChecked(True)
        self.result_stack.setCurrentIndex(_RESULT_VIEW)

    def render_report(self, report: zt.ZhuqueReport, chart_mode: str, *, keep_scroll: bool = False) -> None:
        self._report = report
        self._report_mode = chart_mode
        scroll = self.result_view.verticalScrollBar().value() if keep_scroll else 0
        self._chart_width = self.preferred_chart_width()
        self._chart_dpr = self.current_dpr()

        html, svg = zt.build_report_html(
            report,
            chart_mode=chart_mode,
            chart_image=_CHART_RESOURCE_NAME,
            chart_width=self._chart_width,
        )
        if svg:
            pixmap = _svg_pixmap(svg, self._chart_width, self._chart_dpr)
            if pixmap is None:
                html, _ = zt.build_report_html(report, chart_mode=zt.CHART_UNICODE)
            else:
                self.result_view.document().addResource(
                    QTextDocument.ResourceType.ImageResource, QUrl(_CHART_RESOURCE_NAME), pixmap
                )
        self.result_view.setHtml(html)
        if keep_scroll:
            self.result_view.verticalScrollBar().setValue(scroll)

        raw_html = (
            '<div style="font-size:13px; font-weight:800; color:#0f172a; margin-bottom:6px;">原始返回</div>'
            + zt.json_to_html(report.raw)
        )
        self.raw_view.setHtml(raw_html)
        self.set_actions_enabled(report.ok)

    def sync_chart_render(self) -> None:
        if self._report is None:
            return
        width_changed = abs(self.preferred_chart_width() - self._chart_width) >= 24
        dpr_changed = abs(self.current_dpr() - self._chart_dpr) >= 1e-6
        if not (width_changed or dpr_changed):
            return
        self.render_report(self._report, self._report_mode, keep_scroll=True)

    def _sync_chart_render(self) -> None:
        self.sync_chart_render()

    def request_sync_chart(self) -> None:
        self._chart_timer.start()

    def _on_copy_clicked(self) -> None:
        report = self._report
        if report is None:
            self.status_hint.emit("还没有可复制的检测结果。", "warning")
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            self.status_hint.emit("当前环境不支持剪贴板。", "error")
            return
        mime = QMimeData()
        mime.setHtml(zt.report_to_plain_html(report))
        mime.setText(zt.report_summary_text(report))
        try:
            clipboard.setMimeData(mime)
        except Exception as exc:
            self.status_hint.emit(f"复制失败：{exc}", "error")
            return
        self.status_hint.emit("已复制检测结论（含逐段明细）。", "success")
        show_toast(self, "已复制")

    def _on_export_clicked(self) -> None:
        report = self._report
        if report is None:
            self.status_hint.emit("还没有可导出的检测结果。", "warning")
            return
        from pathlib import Path

        stamp = time.strftime("%Y%m%d_%H%M%S")
        default = str(Path.home() / f"zhuque_result_{stamp}.html")
        path, selected = QFileDialog.getSaveFileName(
            self,
            "导出检测结果",
            default,
            "HTML (*.html);;Markdown (*.md)",
        )
        if not path:
            return
        target = Path(path)
        suffix = target.suffix.lower()
        if suffix not in (".md", ".html", ".htm"):
            suffix = ".html" if "html" in str(selected).lower() else ".md"
            target = target.with_name(target.name + suffix)
        try:
            if suffix in (".html", ".htm"):
                content = zt.report_to_html_document(report, chart_mode=self._report_mode)
            else:
                content = zt.report_to_markdown(report)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            self.status_hint.emit(f"导出失败：{exc}", "error")
            return
        self.status_hint.emit(f"已导出：{target}", "success")
