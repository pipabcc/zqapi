"""朱雀检测标签页：协调待检测编辑、历史记录与结果报告展示。

采用组件化架构：
- 待检测文本编辑：:class:`ZhuqueEditorPanel` (zhuque_editor)
- 历史记录管理：:class:`ZhuqueHistoryPanel` (zhuque_history)
- 检测结论展示：:class:`ZhuqueResultPanel` (zhuque_result)
- 数据与配置层：:class:`ZhuqueStore` (zhuque_store)
"""

from __future__ import annotations

import time
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

from PyQt6.QtCore import QEvent, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui_kit import (
    COMMON_SCROLLBAR_QSS,
    ElidedLabel,
    ask_confirmation,
    install_custom_text_context_menus,
    load_icon,
)
from zhuque_editor import ZhuqueEditorPanel
from zhuque_history import ZhuqueHistoryPanel
from zhuque_result import ZhuqueResultPanel
from zhuque_store import ZhuqueStore, ZhuqueStoreError
import zhuque_text as zt


_STATE_TONES = {
    "info": "#64748b",
    "success": "#15803d",
    "warning": "#b45309",
    "error": "#b91c1c",
}

_TEXT_WARN_THRESHOLD = 30000
_SCREEN_SCALE_SIGNALS = ("devicePixelRatioChanged", "logicalDotsPerInchChanged", "geometryChanged")
_RUNNING_WORKERS: set["_ZhuqueDetectWorker"] = set()


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


class _ZhuqueDetectWorker(QThread):
    """后台调用朱雀检测接口（支持 CancellationToken 物理中断底层传输）。"""

    finished_ok = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)
    status_changed = pyqtSignal(int, str)

    def __init__(
        self,
        request_id: int,
        text: str,
        api_key: str,
        *,
        is_merge: bool,
        upstream: str,
        timeout: int = 60,
        retry_delays: Sequence[float] = zt.RETRY_DELAYS,
    ) -> None:
        super().__init__()
        self._request_id = int(request_id)
        self._text = str(text or "")
        self._api_key = str(api_key or "")
        self._is_merge = bool(is_merge)
        self._upstream = str(upstream or zt.DEFAULT_UPSTREAM)
        self._timeout = int(timeout)
        self._retry_delays = tuple(float(d) for d in (retry_delays or ()))
        self._cancel_token = zt.CancellationToken()

    def cancel(self) -> None:
        self._cancel_token.cancel()

    def _sleep_interruptible(self, seconds: float) -> None:
        steps = max(1, int(float(seconds) / 0.1))
        for _ in range(steps):
            if self._cancel_token.cancelled:
                return
            self.msleep(100)

    def run(self) -> None:
        attempts = len(self._retry_delays) + 1
        last_error = "检测失败。"
        for attempt in range(attempts):
            if self._cancel_token.cancelled:
                self.failed.emit(self._request_id, "已取消检测。")
                return
            if attempt:
                self.status_changed.emit(
                    self._request_id, f"网络不稳定，正在第 {attempt + 1} 次尝试…"
                )
            try:
                payload = zt.call_zhuque(
                    self._text,
                    self._api_key,
                    is_merge=self._is_merge,
                    upstream=self._upstream,
                    timeout=self._timeout,
                    cancel_token=self._cancel_token,
                )
            except zt.ZhuqueError as exc:
                last_error = str(exc) or "检测失败。"
                can_retry = exc.retryable and attempt + 1 < attempts and not self._cancel_token.cancelled
                if can_retry:
                    idx = min(attempt, len(self._retry_delays) - 1)
                    self._sleep_interruptible(self._retry_delays[idx])
                    continue
                self.failed.emit(self._request_id, last_error)
                return
            except Exception as exc:
                self.failed.emit(self._request_id, f"检测失败：{exc}")
                return
            self.finished_ok.emit(self._request_id, payload)
            return
        self.failed.emit(self._request_id, last_error)


class ZhuqueDetectTab(QWidget):
    """「朱雀检测」主页面。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = ZhuqueStore()
        self._worker: Optional[_ZhuqueDetectWorker] = None
        self._request_seq = 0
        self._request_started_at = 0.0
        self._busy = False
        self._watched_screen: Any = None
        self._dpr_watch_installed = False
        self._input_pane_width = 0

        self.setObjectName("ZhuquePage")
        self._build_ui()
        self._apply_style()
        self._load_settings()
        self._history_panel.refresh()
        install_custom_text_context_menus(self, self)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 14)
        root.setSpacing(10)

        root.addWidget(self._build_header())
        root.addWidget(self._build_body(), 1)
        root.addWidget(self._build_status_row())

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        icon_label = QLabel()
        icon_label.setFixedSize(24, 24)
        icon_label.setPixmap(load_icon("icon_zhuque.svg").pixmap(QSize(24, 24)))

        subtitle = QLabel("调用朱雀 AIGC 文本检测接口，逐段判定人工 / AI 成分")
        subtitle.setObjectName("ZhuqueSubtitle")

        layout.addWidget(icon_label)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        return header

    def _build_body(self) -> QWidget:
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setObjectName("ZhuqueSplitter")
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(10)

        # 实例化三大子面板
        self._history_panel = ZhuqueHistoryPanel(self._store, self._splitter)
        self._editor_panel = ZhuqueEditorPanel(self._splitter)
        self._result_panel = ZhuqueResultPanel(self._splitter)

        self._splitter.addWidget(self._history_panel)
        self._splitter.addWidget(self._editor_panel)
        self._splitter.addWidget(self._result_panel)

        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setStretchFactor(2, 2)
        self._splitter.setSizes([224, 400, 880])
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

        # 信号关联
        self._editor_panel.detect_requested.connect(self._on_detect_clicked)
        self._editor_panel.cancel_requested.connect(self._on_cancel_clicked)
        self._editor_panel.clear_requested.connect(self._result_panel.clear)
        self._editor_panel.status_hint.connect(self._set_status)
        self._editor_panel.file_imported.connect(
            lambda name, n: self._set_status(f"已导入 {name}（{n} 字）。", tone="success")
        )

        self._history_panel.record_selected.connect(self._on_history_record_selected)
        self._history_panel.status_hint.connect(self._set_status)

        self._result_panel.segment_anchor_clicked.connect(self._on_segment_anchor_clicked)
        self._result_panel.options_changed.connect(self._on_options_changed)
        self._result_panel.status_hint.connect(self._set_status)

        return self._splitter

    def _build_status_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        api_label = QLabel("api 地址")
        api_label.setObjectName("ZhuqueFooterLabel")
        self._upstream_edit = QLineEdit()
        self._upstream_edit.setObjectName("ZhuqueUpstream")
        self._upstream_edit.setMinimumWidth(280)
        self._upstream_edit.setToolTip("朱雀 classify 接口地址，回车保存。")
        self._upstream_edit.editingFinished.connect(self._on_upstream_edited)

        key_label = QLabel("API Key")
        key_label.setObjectName("ZhuqueFooterLabel")
        self._key_edit = QLineEdit()
        self._key_edit.setObjectName("ZhuqueKey")
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText("EdgeOne Makers API Key（申请见「帮助」）")
        self._key_edit.setMinimumWidth(160)
        self._key_edit.setMaximumWidth(300)
        self._key_edit.setToolTip("只保存在本机 data/zhuque_text.db")
        self._key_edit.editingFinished.connect(self._on_api_key_edited)

        self._key_visible_btn = QToolButton()
        self._key_visible_btn.setObjectName("ZhuqueEye")
        self._key_visible_btn.setText("显示")
        self._key_visible_btn.setCheckable(True)
        self._key_visible_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._key_visible_btn.setMinimumHeight(28)
        self._key_visible_btn.setMinimumWidth(52)
        self._key_visible_btn.toggled.connect(self._on_key_visible_toggled)

        # 动态进度条（长耗时动态反馈）
        self._busy_bar = QProgressBar()
        self._busy_bar.setObjectName("ZhuqueBusyBar")
        self._busy_bar.setRange(0, 0)
        self._busy_bar.setFixedHeight(4)
        self._busy_bar.setFixedWidth(80)
        self._busy_bar.setTextVisible(False)
        self._busy_bar.setVisible(False)
        self._busy_bar.setStyleSheet(
            "QProgressBar#ZhuqueBusyBar { border: none; background: #e2e8f0; border-radius: 2px; } "
            "QProgressBar#ZhuqueBusyBar::chunk { background: #3b82f6; border-radius: 2px; }"
        )

        self._status_label = ElidedLabel("")
        self._status_label.setObjectName("ZhuqueStatus")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(api_label)
        layout.addWidget(self._upstream_edit)
        layout.addWidget(key_label)
        layout.addWidget(self._key_edit)
        layout.addWidget(self._key_visible_btn)
        layout.addWidget(self._busy_bar)
        layout.addWidget(self._status_label, 1)

        self._set_status("就绪：粘贴文本后点击「检测」。", tone="info")
        return row

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"""
            QWidget#ZhuquePage {{ background: transparent; }}
            QWidget {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 13px; color: #111827; }}
            QLabel#ZhuqueSubtitle {{ color: #64748b; font-size: 12px; font-weight: 600; }}
            QLabel#ZhuqueFieldLabel {{ color: #475569; font-size: 12px; font-weight: 700; }}
            QWidget#CardPanel {{ background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px; }}
            QWidget#CardPanelHeader {{ background: transparent; border: none; }}
            QLabel#CardPanelTitle {{ font-size: 13px; font-weight: 800; color: #334155; }}
            QLabel#CardPanelHint {{ color: #94a3b8; font-size: 11px; font-weight: 600; }}
            QToolButton#ZhuqueNewButton {{
                background: #f1f5f9; color: #475569; border: none; border-radius: 12px; padding: 0px;
            }}
            QToolButton#ZhuqueNewButton:hover {{ background: #e2e8f0; color: #0f172a; }}
            QPlainTextEdit#ZhuqueEditor {{
                background: transparent; border: none; padding: 4px 10px 10px 10px; font-size: 13px; color: #111827;
            }}
            QTextBrowser#ZhuqueResult {{ background: transparent; border: none; padding: 4px 12px 10px 12px; }}
            QListWidget#ZhuqueHistoryList {{ background: transparent; border: none; outline: none; padding: 2px 6px; }}
            QToolButton#ZqapiSegBtn {{
                background: transparent; border: none; border-radius: 6px; padding: 3px 12px;
                color: #64748b; font-weight: 700;
            }}
            QToolButton#ZqapiSegBtn:hover {{ background: #eef2f7; color: #334155; }}
            QToolButton#ZqapiSegBtn:checked {{ background: #1e293b; color: #ffffff; }}
            QPushButton#ZhuqueHeaderPrimary {{
                background: #1e293b; color: #ffffff; border: none; border-radius: 8px;
                padding: 0 12px; font-size: 13px; font-weight: 800;
            }}
            QPushButton#ZhuqueHeaderPrimary:hover {{ background: #334155; }}
            QPushButton#ZhuqueHeaderBtn, QPushButton#ZhuqueMini {{
                background: #f1f5f9; color: #334155; border: none; border-radius: 6px;
                padding: 3px 10px; font-size: 12px; font-weight: 700;
            }}
            QPushButton#ZhuqueHeaderBtn:hover, QPushButton#ZhuqueMini:hover {{ background: #e2e8f0; color: #0f172a; }}
            QLineEdit#ZhuqueKey, QLineEdit#ZhuqueUpstream {{
                min-height: 28px; border: 1px solid #dbe3ee; border-radius: 8px; background: #ffffff;
                padding: 2px 10px; color: #111827;
            }}
            QToolButton#ZhuqueEye {{
                min-height: 28px; border: 1px solid #e2e8f0; border-radius: 8px; background: #ffffff;
                padding: 0 10px; color: #475569; font-size: 12px; font-weight: 700;
            }}
            QLabel#ZhuqueStatus {{ font-size: 12px; font-weight: 600; }}
            QLabel#ZhuqueFooterLabel {{ color: #64748b; font-size: 12px; font-weight: 700; }}
            QSplitter#ZhuqueSplitter::handle {{ background: transparent; }}
            QSplitter#ZhuqueSplitter::handle:hover {{ background: rgba(148, 163, 184, 0.25); }}
            {COMMON_SCROLLBAR_QSS}
            """
        )

    # ------------------------------------------------------------------ 配置与状态
    def _set_status(self, text: str, tone: str = "info") -> None:
        self._status_label.setText(str(text))
        self._status_label.setStyleSheet(f"color: {_STATE_TONES.get(tone, _STATE_TONES['info'])};")

    def _load_settings(self) -> None:
        s = self._store.load_settings()
        self._key_edit.setText(str(s.get("api_key") or ""))
        self._upstream_edit.setText(str(s.get("upstream") or zt.DEFAULT_UPSTREAM))

        m_idx = self._result_panel.merge_combo.findData(bool(s.get("is_merge")))
        self._result_panel.merge_combo.blockSignals(True)
        self._result_panel.merge_combo.setCurrentIndex(max(0, m_idx))
        self._result_panel.merge_combo.blockSignals(False)

        c_idx = self._result_panel.chart_combo.findData(str(s.get("chart_mode") or ""))
        self._result_panel.chart_combo.blockSignals(True)
        self._result_panel.chart_combo.setCurrentIndex(max(0, c_idx))
        self._result_panel.chart_combo.blockSignals(False)

        if getattr(self._store, "broken", False):
            self._set_status("历史库不可用（无权限或不可写），检测仍可用但历史不会保存。", tone="error")
        elif getattr(self._store, "recovered", False):
            self._set_status("检测到旧历史库损坏，已自动备份旧文件并重建干净数据库。", tone="warning")
        elif not str(s.get("api_key") or "").strip():
            self._set_status("首次使用：请在底部填写 EdgeOne Makers API Key（参见「帮助」）。", tone="warning")
        else:
            self._set_status(f"已载入本机 API Key：{zt.mask_key(str(s.get('api_key')))}", tone="info")

    def _on_key_visible_toggled(self, visible: bool) -> None:
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self._key_visible_btn.setText("隐藏" if visible else "显示")

    def _on_upstream_edited(self) -> None:
        url = self._upstream_edit.text().strip() or zt.DEFAULT_UPSTREAM
        if not _is_http_url(url):
            saved = str(self._store.load_settings().get("upstream") or zt.DEFAULT_UPSTREAM)
            self._upstream_edit.setText(saved)
            self._set_status(f"api 地址格式错误，已恢复为：{saved}", tone="warning")
            return
        self._store.save_settings({"upstream": url})
        self._set_status(f"已保存 api 地址：{url}", tone="success")

    def _on_api_key_edited(self) -> None:
        key = self._key_edit.text().strip()
        self._store.save_settings({"api_key": key})
        if key:
            self._set_status(f"已保存 API Key：{zt.mask_key(key)}", tone="success")
        else:
            self._set_status("API Key 已清空，检测前需重新填写。", tone="warning")

    def _on_options_changed(self) -> None:
        merge_val = bool(self._result_panel.merge_combo.currentData())
        chart_val = str(self._result_panel.chart_combo.currentData() or zt.DEFAULT_CHART_MODE)
        self._store.save_settings({"is_merge": merge_val, "chart_mode": chart_val})
        if self._result_panel._report is not None and not self._busy:
            self._result_panel.render_report(self._result_panel._report, chart_val)

    # ------------------------------------------------------------------ 分栏宽度保持
    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        widget = self._splitter.widget(1)
        if widget is not None:
            self._input_pane_width = widget.width()

    def _pin_input_pane_width_soon(self) -> None:
        QTimer.singleShot(0, self._pin_input_pane_width)

    def _pin_input_pane_width(self) -> None:
        widget = self._splitter.widget(1)
        if widget is None or self._input_pane_width <= 0:
            if widget is not None and self._input_pane_width <= 0:
                self._input_pane_width = widget.width()
            return
        sizes = self._splitter.sizes()
        if len(sizes) >= 3 and sizes[1] != self._input_pane_width:
            delta = self._input_pane_width - sizes[1]
            sizes[1] = self._input_pane_width
            sizes[2] = max(240, sizes[2] - delta)
            self._splitter.setSizes(sizes)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._pin_input_pane_width_soon()

    # ------------------------------------------------------------------ 检测业务
    def _on_detect_clicked(self) -> None:
        text = self._editor_panel.text()
        if not text.strip():
            self._set_status("请先粘贴需要检测的文本。", tone="warning")
            self._editor_panel.text_edit.setFocus()
            return
        key = self._key_edit.text().strip()
        if not key:
            self._set_status("请先填写 API Key（参考「帮助」）。", tone="warning")
            self._key_edit.setFocus()
            return

        # 文本超长防呆提示
        if len(text) > _TEXT_WARN_THRESHOLD:
            ok = ask_confirmation(
                self,
                "超长文本提示",
                f"当前文本共 {len(text)} 字，超过推荐的 {_TEXT_WARN_THRESHOLD} 字限制。\n接口可能会超时或报错，是否继续？",
            )
            if not ok:
                return

        if self._worker is not None and self._worker.isRunning():
            return

        is_merge = bool(self._result_panel.merge_combo.currentData())
        chart_mode = str(self._result_panel.chart_combo.currentData() or zt.DEFAULT_CHART_MODE)
        upstream = self._upstream_edit.text().strip() or zt.DEFAULT_UPSTREAM

        self._store.save_settings(
            {"api_key": key, "is_merge": is_merge, "chart_mode": chart_mode, "upstream": upstream}
        )

        self._request_seq += 1
        self._busy = True
        self._editor_panel.set_busy(True)
        self._busy_bar.setVisible(True)
        self._request_started_at = time.monotonic()
        self._set_status(f"正在请求朱雀接口…（检测 {len(text)} 字）", tone="info")

        worker = _ZhuqueDetectWorker(
            self._request_seq,
            text,
            key,
            is_merge=is_merge,
            upstream=upstream,
        )
        self._worker = worker
        _RUNNING_WORKERS.add(worker)
        worker.finished_ok.connect(self._on_detect_finished)
        worker.failed.connect(self._on_detect_failed)
        worker.status_changed.connect(lambda req_id, msg: self._set_status(msg, tone="info"))
        worker.finished.connect(lambda w=worker: _RUNNING_WORKERS.discard(w))
        worker.start()

    def _on_cancel_clicked(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                for sig in (self._worker.finished_ok, self._worker.failed):
                    sig.disconnect(self)
            except Exception:
                pass
        self._worker = None
        self._request_seq += 1
        self._finish_busy()
        self._set_status("已物理中断检测请求（不计入历史）。", tone="info")

    def _finish_busy(self) -> None:
        self._busy = False
        self._editor_panel.set_busy(False)
        self._busy_bar.setVisible(False)

    def _on_detect_finished(self, request_id: int, payload: object) -> None:
        if request_id != self._request_seq:
            return
        self._finish_busy()
        if not isinstance(payload, dict):
            self._on_detect_failed(request_id, "上游返回结构异常。")
            return

        text = self._editor_panel.text()
        report = zt.parse_report(payload, text)
        chart_mode = str(self._result_panel.chart_combo.currentData() or zt.DEFAULT_CHART_MODE)
        self._result_panel.render_report(report, chart_mode)

        if not report.ok:
            self._editor_panel.clear_highlights()
            self._set_status(f"上游 status = {report.status}，未写入历史，请查看「原始返回」。", tone="warning")
            return

        self._editor_panel.apply_highlights(report)

        elapsed = round(time.monotonic() - self._request_started_at, 1) if self._request_started_at else 0
        elapsed_str = f"耗时 {elapsed} 秒" if elapsed else ""

        try:
            record = self._store.add_record(
                text=text,
                payload=payload,
                summary=text.strip().replace("\n", " ")[:60],
                verdict=report.verdict_text,
                level=report.verdict_level,
                ai_score=report.softmax,
                ratio=report.ratio,
                human=report.human,
                is_merge=bool(self._result_panel.merge_combo.currentData()),
                chart_mode=chart_mode,
            )
            self._history_panel.refresh(select_id=str(record.get("id") or ""))
        except ZhuqueStoreError as exc:
            self._set_status(f"检测完成，但历史记录写入失败：{exc}", tone="error")
            return

        self._pin_input_pane_width_soon()
        detail = "，".join(p for p in (report.verdict_text, elapsed_str) if p)
        self._set_status(
            f"检测完成：疑似 AI 占比 {zt.pct(report.ratio)}，整体 AI 置信度 {zt.pct(report.softmax)}（{detail}）",
            tone="success",
        )

    def _on_detect_failed(self, request_id: int, message: str) -> None:
        if request_id != self._request_seq:
            return
        self._finish_busy()
        self._editor_panel.clear_highlights()
        self._result_panel.display_failure(message)
        self._set_status(f"{message}", tone="error")

    def _on_history_record_selected(self, record_id: str) -> None:
        rec = self._store.get_record(record_id)
        if rec is None:
            return
        payload = rec.get("payload") or {}
        text = str(rec.get("text") or "")
        self._editor_panel.set_text(text)

        report = zt.parse_report(payload, text)
        mode = str(rec.get("chart_mode") or self._result_panel.chart_combo.currentData() or zt.DEFAULT_CHART_MODE)
        self._result_panel.render_report(report, mode)
        self._editor_panel.apply_highlights(report)
        self._pin_input_pane_width_soon()
        self._set_status(
            f"已载入历史记录（{rec.get('created_at')}）：疑似 AI 占比 {zt.pct(report.ratio)}，整体 AI 置信度 {zt.pct(report.softmax)}",
            tone="info",
        )

    def _on_segment_anchor_clicked(self, order: int) -> None:
        self._editor_panel.jump_to_segment(order)
        self._set_status(f"已定位到第 {order} 段。", tone="info")

    # ------------------------------------------------------------------ 生命周期
    def on_shown(self) -> None:
        self._editor_panel._refresh_count_hint()
        self._pin_input_pane_width_soon()
        self._result_panel.request_sync_chart()

    def cleanup(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(200)
        self._store.close()
