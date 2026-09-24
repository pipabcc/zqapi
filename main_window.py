"""主窗口：把「文本比较」与「朱雀检测」作为同一窗口下的同级标签页。

顶部是标签切换，下面就是各自原本的操作界面；两边互不干扰 —— 各自的线程、各自的历史
与配置，切过去就是各自独立运行。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QApplication, QTabWidget, QVBoxLayout, QWidget

from compare_tab import TextCompareTab
from help_tab import HelpTab
from ui_kit import MAIN_WINDOW_BACKGROUND, app_icon, apply_caption_color, load_icon, polish_combo_popups
from zhuque_tab import ZhuqueDetectTab


_TAB_ZHUQUE = 0
_TAB_COMPARE = 1
_TAB_HELP = 2

# 每个标签的（默认图标，选中态图标）：选中态是深色底，要用白色版
_TAB_ICONS = (
    ("icon_zhuque.svg", "icon_zhuque_white.svg"),
    ("icon_compare.svg", "icon_compare_white.svg"),
    ("icon_help.svg", "icon_help_white.svg"),
)


class ToolboxWindow(QWidget):
    """两个工具的宿主窗口。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("ToolboxRoot")
        self.setWindowTitle("AI写作工具箱")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setMinimumSize(960, 640)
        self.setWindowIcon(app_icon())
        self._fit_to_screen()

        self.zhuque_tab = ZhuqueDetectTab(self)
        self.compare_tab = TextCompareTab(self)
        self.help_tab = HelpTab(self)
        self._build_ui()
        self._apply_style()

        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._refresh_tab_icons()
        # 界面装完之后统一修一遍下拉弹窗（老版本 Qt 会露黑角，见 ui_kit.polish_combo_popup）
        polish_combo_popups(self)
        self._restore_window_state()

    # ------------------------------------------------------------------ 状态持久化
    def _restore_window_state(self) -> None:
        try:
            from zhuque_store import ZhuqueStore

            store = ZhuqueStore()
            state = store.get_custom_setting("main_window_state", None)
            if isinstance(state, dict):
                w = state.get("width")
                h = state.get("height")
                if isinstance(w, int) and isinstance(h, int) and w >= 960 and h >= 640:
                    self.resize(w, h)
                tab_idx = state.get("current_tab_index")
                if isinstance(tab_idx, int) and 0 <= tab_idx < self.tabs.count():
                    self.tabs.setCurrentIndex(tab_idx)
                if state.get("is_maximized"):
                    self.showMaximized()
        except Exception:
            pass

    def _save_window_state(self) -> None:
        try:
            from zhuque_store import ZhuqueStore

            store = ZhuqueStore()
            state = {
                "width": self.width(),
                "height": self.height(),
                "is_maximized": self.isMaximized(),
                "current_tab_index": self.tabs.currentIndex(),
            }
            store.set_custom_setting("main_window_state", state)
        except Exception:
            pass

    # ------------------------------------------------------------------ 构建
    def _fit_to_screen(self) -> None:
        """默认尺寸按屏幕可用区收缩，小屏上不会一开就超出可视范围。"""
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        if available is None or available.width() <= 0 or available.height() <= 0:  # pragma: no cover
            self.resize(1420, 880)
            return
        width = min(1440, int(available.width() * 0.94))
        height = min(900, int(available.height() * 0.92))
        self.resize(max(960, width), max(640, height))

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(0)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("ToolboxTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setIconSize(QSize(16, 16))
        # 去掉标签栏底下那条贯穿整行的基准线，以及相邻标签之间的竖分隔线
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.addTab(self.zhuque_tab, load_icon("icon_zhuque.svg"), "朱雀检测")
        self.tabs.addTab(self.compare_tab, load_icon("icon_compare.svg"), "文本比较")
        self.tabs.addTab(self.help_tab, load_icon("icon_help.svg"), "帮助")
        self.tabs.setCurrentIndex(_TAB_ZHUQUE)
        root.addWidget(self.tabs, 1)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget#ToolboxRoot { background: __MAIN_BACKGROUND__; }
            QTabWidget#ToolboxTabs { background: __MAIN_BACKGROUND__; }
            QTabWidget#ToolboxTabs::pane { border: none; background: __MAIN_BACKGROUND__; top: 2px; }
            /* 标签栏整体右移，让第一个标签的左边缘与页面里那枚 24px 图标的左边缘对齐 */
            QTabWidget#ToolboxTabs::tab-bar { left: 18px; }
            QTabBar { background: transparent; border: none; }
            QTabBar::tab {
                background: transparent; color: #64748b; font-size: 13px; font-weight: 800;
                padding: 7px 18px; margin: 0 6px 4px 0; border: none; border-radius: 8px;
            }
            QTabBar::tab:hover { background: #eef2f7; color: #334155; }
            QTabBar::tab:selected { background: #1e293b; color: #ffffff; }
            """.replace("__MAIN_BACKGROUND__", MAIN_WINDOW_BACKGROUND)
        )

    # ------------------------------------------------------------------ 行为
    def _refresh_tab_icons(self) -> None:
        """选中态是深色底，选中时把图标换成白色版本，否则深色图标看不见。"""
        index = self.tabs.currentIndex()
        for position, (normal, active) in enumerate(_TAB_ICONS):
            self.tabs.setTabIcon(position, load_icon(active if position == index else normal))

    def _on_tab_changed(self, index: int) -> None:
        self._refresh_tab_icons()
        tab = self.tabs.widget(int(index))
        # 切到前台时各自刷新一次（字数统计、分隔条宽度回位、图表按当前宽度重画）
        handler = getattr(tab, "on_shown", None)
        if callable(handler):
            try:
                handler()
            except RuntimeError:  # pragma: no cover - 页面已销毁
                pass

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        apply_caption_color(self)
        handler = getattr(self.tabs.currentWidget(), "on_shown", None)
        if callable(handler):
            handler()
        self._refresh_tab_icons()

    def cleanup(self) -> None:
        for tab in (self.zhuque_tab, self.compare_tab, self.help_tab):
            handler = getattr(tab, "cleanup", None)
            if callable(handler):
                try:
                    handler()
                except Exception:  # pragma: no cover - 兜底
                    pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._save_window_state()
        self.cleanup()
        super().closeEvent(event)
