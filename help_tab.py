"""帮助标签页：申请朱雀检测 API Key 的步骤说明。

只做一件事：把"去哪儿申请、在控制台点哪里、复制到本程序的哪个输入框"讲清楚，
并提供一条可点击（用系统默认浏览器打开）的控制台链接和一张控制台截图。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QSize, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui_kit import install_custom_text_context_menus, load_icon, show_toast
from app_paths import get_assets_dir


CONSOLE_URL = "https://console.cloud.tencent.com/edgeone/makers?tab=models&subTab=overview"

_HELP_IMAGE = "help-edgeone-models.png"


class _ScaledImageLabel(QLabel):
    """按可用宽度等比缩放的图片标签（不放大超过原图，按物理像素出图保持清晰）。"""

    def __init__(self, source: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source = source
        self.setObjectName("HelpImage")
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt 命名
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt 命名
        if self._source.isNull() or self._source.width() <= 0:
            return 0
        return max(1, int(round(self._source.height() * min(width, self._source.width()) / self._source.width())))

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(self._source.width(), self._source.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        if self._source.isNull():
            return
        # 宽度上限不超过原图，避免糊
        logical = max(1, min(int(self.width()), self._source.width()))
        try:
            dpr = float(self.devicePixelRatioF()) or 1.0
        except (TypeError, ValueError):  # pragma: no cover - 极端情况
            dpr = 1.0
        scaled = self._source.scaledToWidth(
            max(1, int(round(logical * dpr))), Qt.TransformationMode.SmoothTransformation
        )
        scaled.setDevicePixelRatio(dpr)
        super().setPixmap(scaled)


class HelpTab(QWidget):
    """「帮助」标签页。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HelpPage")
        self._build_ui()
        self._apply_style()
        install_custom_text_context_menus(self, self)

    # ------------------------------------------------------------------ 构建
    def _asset_icon(self, name: str, fallback: Optional[QIcon] = None) -> QIcon:
        icon = load_icon(name)
        if icon.isNull() and fallback is not None:
            return fallback
        return icon

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 14)
        root.setSpacing(10)
        root.addWidget(self._build_header())

        scroll = QScrollArea()
        scroll.setObjectName("HelpScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._build_content())
        root.addWidget(scroll, 1)

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        icon_label = QLabel()
        icon_label.setFixedSize(24, 24)
        icon_label.setPixmap(self._asset_icon("icon_help.svg").pixmap(QSize(24, 24)))

        subtitle = QLabel("申请朱雀检测 API Key 的步骤：去腾讯云 EdgeOne Makers 建一个 Key，再填回本程序")
        subtitle.setObjectName("HelpSubtitle")

        layout.addWidget(icon_label)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        return header

    def _build_content(self) -> QWidget:
        content = QWidget()
        content.setObjectName("HelpContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        card = QWidget()
        card.setObjectName("HelpCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 20)
        card_layout.setSpacing(16)

        card_layout.addWidget(
            self._step(
                1,
                "打开腾讯云 EdgeOne Makers 控制台，免费使用朱雀检测 API",
                "登录腾讯云账号后访问下面的地址（点击即可用系统默认浏览器打开）：",
                extra=self._build_link_row(),
            )
        )
        card_layout.addWidget(
            self._step(
                2,
                "切到「Makers → Models」，点击「API Key」",
                "在顶部「Makers」下选中「Models」，再点左侧菜单里的「API Key」；"
                "然后点击「创建 API Key」按钮，把创建好的 API Key 复制下来。"
                "（Key 只在创建时完整可见，请当场保存。）",
                extra=self._build_image_block(),
            )
        )
        card_layout.addWidget(
            self._step(
                3,
                "把 API Key 填回本程序",
                "回到本程序的「朱雀检测」标签页，把复制好的 API Key 粘贴到页面底部工具栏的 "
                "「API Key」输入框里（输入完点一下别处即自动保存）。",
            )
        )
        card_layout.addWidget(
            self._step(
                4,
                "开始检测",
                "在「待检测文本」区域输入或粘贴要检测的内容，按标题行右侧的「检测」按钮开始检测；"
                "结果出来后可以在右侧「可视图表 / 原始返回」之间切换，也可以复制或导出。",
            )
        )
        layout.addWidget(card)

        note = QLabel(
            '提示：API Key 只保存在本机 <span style="color:#475569;">data/zhuque_text.db</span> 里，'
            "程序不内置任何密钥；换机器或删掉这个文件后需要重新填写。"
        )
        note.setObjectName("HelpNote")
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return content

    def _step(self, number: int, title: str, body: str, *, extra: Optional[QWidget] = None) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        badge = QLabel(str(number))
        badge.setObjectName("HelpStepBadge")
        badge.setFixedSize(22, 22)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        text_box = QWidget()
        text_layout = QVBoxLayout(text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(6)

        title_label = QLabel(title)
        title_label.setObjectName("HelpStepTitle")
        title_label.setWordWrap(True)
        body_label = QLabel(body)
        body_label.setObjectName("HelpStepBody")
        body_label.setWordWrap(True)

        text_layout.addWidget(title_label)
        text_layout.addWidget(body_label)
        if extra is not None:
            text_layout.addWidget(extra)

        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(text_box, 1)
        return row

    def _build_link_row(self) -> QWidget:
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(4)

        self._link_label = QLabel(
            '<a href="%s" style="color:#2563eb;">%s</a>' % (CONSOLE_URL, CONSOLE_URL)
        )
        self._link_label.setObjectName("HelpLink")
        self._link_label.setTextFormat(Qt.TextFormat.RichText)
        self._link_label.setWordWrap(True)
        self._link_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._link_label.setCursor(Qt.CursorShape.PointingHandCursor)
        # 链接不要走"自动打开"，自己接管以便失败时给提示
        self._link_label.setOpenExternalLinks(False)
        self._link_label.linkActivated.connect(self._on_link_activated)
        palette = self._link_label.palette()
        palette.setColor(QPalette.ColorRole.Link, QColor("#2563eb"))
        self._link_label.setPalette(palette)

        hint = QLabel("（用系统默认浏览器打开；也可以选中上面的地址手动复制）")
        hint.setObjectName("HelpLinkHint")
        hint.setWordWrap(True)

        layout.addWidget(self._link_label)
        layout.addWidget(hint)
        return row

    def _build_image_block(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(6)

        source = QPixmap(str(get_assets_dir() / _HELP_IMAGE))
        if source.isNull():
            fallback = QLabel("（示意图加载失败：assets/%s 不存在）" % _HELP_IMAGE)
            fallback.setObjectName("HelpStepHint")
            layout.addWidget(fallback)
            return box

        frame = QWidget()
        frame.setObjectName("HelpImageFrame")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(8, 8, 8, 8)
        frame_layout.addWidget(_ScaledImageLabel(source))

        caption = QLabel("图中：① 选中顶部「Models」标签　② 点左侧「API Key」后点「创建 API Key」")
        caption.setObjectName("HelpStepHint")
        caption.setWordWrap(True)

        layout.addWidget(frame)
        layout.addWidget(caption)
        return box

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget#HelpPage { background: transparent; }
            QWidget { font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 13px; color: #111827; }
            QLabel#HelpSubtitle { color: #64748b; font-size: 12px; font-weight: 600; }
            QWidget#HelpContent { background: transparent; }
            QWidget#HelpCard { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px; }
            QLabel#HelpStepBadge {
                background: #1e293b; color: #ffffff; border-radius: 11px;
                font-size: 12px; font-weight: 800;
            }
            QLabel#HelpStepTitle { font-size: 14px; font-weight: 800; color: #0f172a; }
            QLabel#HelpStepBody { font-size: 13px; color: #475569; line-height: 170%; }
            QLabel#HelpStepHint { font-size: 12px; color: #94a3b8; }
            QLabel#HelpLink { font-size: 13px; }
            QLabel#HelpLinkHint { font-size: 12px; color: #94a3b8; }
            QWidget#HelpImageFrame { background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px; }
            QLabel#HelpImage { background: transparent; }
            QLabel#HelpNote {
                background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 8px;
                padding: 10px 14px; font-size: 12px; color: #64748b; line-height: 170%;
            }
            QScrollArea#HelpScroll { background: transparent; border: none; }
            QScrollArea#HelpScroll > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical { border: none; background: transparent; width: 8px; margin: 0px; }
            QScrollBar::handle:vertical { background: rgba(100, 116, 139, 0.25); border-radius: 4px; min-height: 24px; }
            QScrollBar::handle:vertical:hover { background: rgba(100, 116, 139, 0.45); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; background: none; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
            """
        )

    # ------------------------------------------------------------------ 行为
    def _on_link_activated(self, url: str) -> None:
        target = str(url or "").strip()
        if not target:
            return
        if QDesktopServices.openUrl(QUrl(target)):
            show_toast(self, "已用默认浏览器打开控制台")
        else:  # pragma: no cover - 极少见
            show_toast(self, "打开浏览器失败，请手动复制上方地址")

    def open_console(self) -> bool:
        """供外部（比如自检）直接触发打开逻辑。"""
        return QDesktopServices.openUrl(QUrl(CONSOLE_URL))

    def on_shown(self) -> None:
        """页面切到前台时调用（这里没有需要刷新的状态，留空以对齐接口）。"""

    def cleanup(self) -> None:
        """主窗口关闭时调用（帮助页没有后台线程与连接，无需处理）。"""
