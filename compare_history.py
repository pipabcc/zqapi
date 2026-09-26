"""文本比较-历史记录管理子组件。

包含轻量自绘的历史比对列表（QStyledItemDelegate）、微型搜索与筛选栏、分页加载与右键菜单。
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QPoint, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFontMetrics,
    QPainter,
)
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QWidget,
)

from ui_kit import (
    CardPanel,
    SimpleMenuPopup,
    install_custom_text_context_menus,
    load_icon,
    make_combo,
)
from zhuque_store import ZhuqueStore, ZhuqueStoreError


_PAGE_SIZE = 30
_ROW_HEIGHT = 46


def _similarity_color(sim: float) -> str:
    """根据相似度分档返回指示颜色：高相似绿色、中相似橙色、低相似蓝色。"""
    if sim >= 0.8:
        return "#1D9E75"
    if sim >= 0.4:
        return "#EF9F27"
    return "#3B82F6"


class _CompareHistoryItemDelegate(QStyledItemDelegate):
    """高性能自绘委托：绘制文本比对历史条目。"""

    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:
        return QSize(option.rect.width(), _ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 选中与悬停背景
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        rect = option.rect.adjusted(2, 2, -2, -2)

        if is_selected:
            painter.setBrush(QColor("#e5edf8"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 6, 6)
        elif is_hover:
            painter.setBrush(QColor(15, 23, 42, 12))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 6, 6)

        data = index.data(Qt.ItemDataRole.UserRole + 1)
        if not isinstance(data, dict):
            painter.restore()
            return

        created = str(data.get("created_at") or "")
        stamp = created[5:16].replace("-", "/") if len(created) >= 16 else created
        similarity = float(data.get("similarity") or 0.0)
        sim_pct = f"{int(round(similarity * 100))}%"
        diff_count = int(data.get("diff_count") or 0)
        orig_chars = int(data.get("orig_chars") or 0)
        rewr_chars = int(data.get("rewr_chars") or 0)
        pinned = bool(data.get("pinned"))
        summary = str(data.get("summary") or "")

        # 1. 相似度色点
        dot_color = _similarity_color(similarity)
        dot_x = rect.left() + 8
        dot_y = rect.top() + 10
        painter.setBrush(QColor(dot_color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(dot_x, dot_y, 8, 8)

        content_left = dot_x + 16
        content_width = max(10, rect.right() - content_left - 8)

        # 2. 标题行（12px 粗体）
        title_font = painter.font()
        title_font.setPixelSize(12)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor("#1e293b"))

        pin_str = "置顶 · " if pinned else ""
        raw_title = f"{pin_str}{stamp} · 相似度 {sim_pct} · {diff_count} 处差异"
        title_metrics = QFontMetrics(title_font)
        elided_title = title_metrics.elidedText(raw_title, Qt.TextElideMode.ElideRight, content_width)
        painter.drawText(content_left, rect.top() + 17, elided_title)

        # 3. 摘要行（11px 常规）
        sub_font = painter.font()
        sub_font.setPixelSize(11)
        sub_font.setBold(False)
        painter.setFont(sub_font)
        painter.setPen(QColor("#94a3b8"))

        raw_meta = f"{orig_chars}字 → {rewr_chars}字 · {summary}" if summary else f"{orig_chars}字 → {rewr_chars}字"
        sub_metrics = QFontMetrics(sub_font)
        elided_meta = sub_metrics.elidedText(raw_meta, Qt.TextElideMode.ElideRight, content_width)
        painter.drawText(content_left, rect.top() + 33, elided_meta)

        painter.restore()


class CompareHistoryPanel(CardPanel):
    """文本比对历史管理面板。"""

    record_selected = pyqtSignal(str)   # record_id
    status_hint = pyqtSignal(str, str)   # message, tone

    def __init__(self, store: ZhuqueStore, parent: QWidget | None = None) -> None:
        super().__init__("历史记录", parent)
        self._store = store
        self._loaded_count = _PAGE_SIZE
        self._raw_records: list[dict[str, Any]] = []

        self._build_header_actions()
        self._build_filter_bar()
        self._build_list()

    def _build_header_actions(self) -> None:
        # 清空历史垃圾桶按钮
        self.clear_all_btn = QToolButton(self.header)
        self.clear_all_btn.setObjectName("CompareHistoryClearBtn")
        self.clear_all_btn.setIcon(load_icon("icon_trash.svg"))
        self.clear_all_btn.setIconSize(QSize(16, 16))
        self.clear_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_all_btn.setFixedSize(24, 24)
        self.clear_all_btn.setToolTip("清空全部文本比较历史（会先确认）")
        self.clear_all_btn.setAccessibleName("清空比对历史")
        self.clear_all_btn.clicked.connect(self._on_clear_all_clicked)
        self.header_layout.addWidget(self.clear_all_btn)

    def _build_filter_bar(self) -> None:
        """微型搜索与筛选栏。"""
        filter_widget = QWidget(self)
        filter_layout = QHBoxLayout(filter_widget)
        filter_layout.setContentsMargins(10, 4, 10, 6)
        filter_layout.setSpacing(6)

        self.search_edit = QLineEdit(filter_widget)
        self.search_edit.setObjectName("CompareHistorySearch")
        self.search_edit.setPlaceholderText("搜索比对…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        self.search_edit.setStyleSheet(
            """
            QLineEdit#CompareHistorySearch {
                min-height: 32px; max-height: 34px; border: 1px solid #e2e8f0; border-radius: 8px;
                padding: 2px 10px; font-size: 12.5px; background: #f8fafc; color: #334155;
            }
            QLineEdit#CompareHistorySearch:focus { border-color: #94a3b8; background: #ffffff; }
            """
        )

        install_custom_text_context_menus(self, self.search_edit)

        self.sim_combo = make_combo(filter_widget, "CompareHistoryFilterCombo")
        combo_view = QListView()
        combo_view.setObjectName("CompareHistoryFilterPopupView")
        self.sim_combo.setView(combo_view)
        self.sim_combo.addItem("全部", "")
        self.sim_combo.addItem("高相似(≥80%)", "high")
        self.sim_combo.addItem("中相似(40-80%)", "mid")
        self.sim_combo.addItem("低相似(<40%)", "low")
        self.sim_combo.currentIndexChanged.connect(self._apply_filter)
        self.sim_combo.setFixedWidth(98)

        filter_layout.addWidget(self.search_edit, 1)
        filter_layout.addWidget(self.sim_combo, 0)
        self.add_content(filter_widget, stretch=0)

    def _build_list(self) -> None:
        self.list_widget = QListWidget(self)
        self.list_widget.setObjectName("CompareHistoryList")
        self.list_widget.setItemDelegate(_CompareHistoryItemDelegate(self.list_widget))
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        self.list_widget.itemActivated.connect(self._on_item_clicked)
        self.list_widget.verticalScrollBar().valueChanged.connect(self._on_scrolled)

        self.load_more_btn = QPushButton("加载更多", self)
        self.load_more_btn.setObjectName("CompareHistoryMini")
        self.load_more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_more_btn.setVisible(False)
        self.load_more_btn.clicked.connect(self._on_load_more)

        self.add_content(self.list_widget, stretch=1)
        self.add_content(self.load_more_btn, stretch=0)

    def refresh(self, select_id: str = "") -> None:
        records = self._store.list_compare_records(limit=max(_PAGE_SIZE, self._loaded_count))
        self._raw_records = records
        self.list_widget.clear()

        selected_item: Optional[QListWidgetItem] = None
        for record in records:
            item = QListWidgetItem()
            rec_id = str(record.get("id") or "")
            item.setData(Qt.ItemDataRole.UserRole, rec_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, record)

            stamp = str(record.get("created_at") or "")
            similarity = float(record.get("similarity") or 0.0)
            diff_count = int(record.get("diff_count") or 0)
            orig_chars = int(record.get("orig_chars") or 0)
            rewr_chars = int(record.get("rewr_chars") or 0)
            summary = str(record.get("summary") or "")

            item.setToolTip(
                f"时间：{stamp}\n"
                f"相似度：{int(round(similarity * 100))}%\n"
                f"差异处数：{diff_count}\n"
                f"字数对比：原文 {orig_chars} 字 → 改写 {rewr_chars} 字\n"
                f"摘要：{summary}"
            )
            self.list_widget.addItem(item)
            if select_id and rec_id == select_id:
                selected_item = item

        self._apply_filter()

        shown = len(records)
        total = shown if shown < self._loaded_count else self._store.count_compare_records()
        has_more = total > shown
        if total <= 0:
            self.set_hint("暂无")
        elif has_more:
            self.set_hint(f"已显示 {shown} / {total} 条")
        else:
            self.set_hint(f"{total} 条")
        self.load_more_btn.setVisible(has_more)

        if selected_item is not None:
            self.list_widget.setCurrentItem(selected_item)

    def _apply_filter(self) -> None:
        keyword = self.search_edit.text().strip().lower()
        sim_filter = str(self.sim_combo.currentData() or "")

        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            data = item.data(Qt.ItemDataRole.UserRole + 1)
            if not isinstance(data, dict):
                continue

            similarity = float(data.get("similarity") or 0.0)
            if sim_filter == "high" and similarity < 0.8:
                item.setHidden(True)
                continue
            if sim_filter == "mid" and (similarity < 0.4 or similarity >= 0.8):
                item.setHidden(True)
                continue
            if sim_filter == "low" and similarity >= 0.4:
                item.setHidden(True)
                continue

            if keyword:
                summary = str(data.get("summary") or "").lower()
                created = str(data.get("created_at") or "").lower()
                if keyword not in summary and keyword not in created:
                    item.setHidden(True)
                    continue

            item.setHidden(False)

    def _on_load_more(self) -> None:
        self._loaded_count += _PAGE_SIZE
        self.refresh()

    def _on_scrolled(self, value: int) -> None:
        bar = self.list_widget.verticalScrollBar()
        if bar.maximum() <= 0 or not self.load_more_btn.isVisible():
            return
        if value >= bar.maximum() - 8:
            self._on_load_more()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        rec_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if rec_id:
            self.record_selected.emit(rec_id)

    def _show_context_menu(self, pos: QPoint) -> None:
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        self.list_widget.setCurrentItem(item)
        rec_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        data = item.data(Qt.ItemDataRole.UserRole + 1)
        if not rec_id or not isinstance(data, dict):
            return
        pinned = bool(data.get("pinned"))

        items = [
            (
                "取消置顶" if pinned else "置顶",
                lambda: self._toggle_pin(rec_id, not pinned),
                True,
            ),
            ("-", None, False),
            ("删除这条记录", lambda: self._delete_record(rec_id), True),
        ]
        popup = SimpleMenuPopup(items, parent=self.list_widget)
        self._active_context_menu = popup
        popup.show_at_pos(self.list_widget.viewport().mapToGlobal(pos))

    def _toggle_pin(self, rec_id: str, pinned: bool) -> None:
        try:
            self._store.set_compare_pinned(rec_id, pinned)
            self.refresh(select_id=rec_id)
            self.status_hint.emit("已置顶这条比对记录。" if pinned else "已取消置顶。", "success")
        except ZhuqueStoreError as exc:
            self.status_hint.emit(f"置顶失败：{exc}", "error")

    def _delete_record(self, rec_id: str) -> None:
        try:
            self._store.delete_compare_record(rec_id)
            self.refresh()
            self.status_hint.emit("已删除这条比对历史记录。", "info")
        except ZhuqueStoreError as exc:
            self.status_hint.emit(f"删除失败：{exc}", "error")

    def _on_clear_all_clicked(self) -> None:
        from ui_kit import ask_confirmation

        total = self._store.count_compare_records()
        if total <= 0:
            self.status_hint.emit("比对历史记录已经是空的。", "info")
            return
        confirmed = ask_confirmation(
            self,
            "清空比对历史",
            f"确定要清空全部 {total} 条文本比较历史记录吗？\n\n风险级别：不可恢复\n影响范围：\n- 全部历史比对记录（含原文与改写文）\n\n点「是」清空，点「否」取消。",
            warning=True,
        )
        if not confirmed:
            self.status_hint.emit("已取消清空比对历史记录。", "info")
            return
        try:
            removed = self._store.clear_compare_records()
            self.refresh()
            self.status_hint.emit(f"已清空 {removed} 条比对历史记录。", "info")
        except ZhuqueStoreError as exc:
            self.status_hint.emit(f"清空失败：{exc}", "error")
