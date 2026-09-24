"""「AI写作工具箱」程序入口。

把「朱雀检测」「文本比较」和「帮助」放在同一个窗口下的三个同级标签页里，各自独立运行，
可以单独打包成 exe 分发。

用法::

    python main.py                  # 正常启动
    python main.py --self-check     # 自检：构建界面、抓两张标签页截图后退出（打包后验证用）
    python main.py --startup-probe   # 启动探针：按正常路径启动，把窗口状态写成 json 后退出
"""

from __future__ import annotations

import os
import sys

# 打包成 windowed exe 后 stdout / stderr 可能是 None，部分库在异常路径上会向它们写入
if getattr(sys, "frozen", False):
    try:
        if sys.stdout is None:
            import io

            sys.stdout = io.StringIO()
        if sys.stderr is None:
            import io

            sys.stderr = io.StringIO()
    except Exception:
        pass

from PyQt6.QtCore import QLibraryInfo, QTimer, QTranslator  # noqa: E402
from PyQt6.QtGui import QColor, QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from app_paths import get_app_dir  # noqa: E402
from main_window import ToolboxWindow  # noqa: E402
from ui_kit import MAIN_WINDOW_BACKGROUND, app_icon  # noqa: E402


def _install_chinese_translations(app: QApplication) -> None:
    """装上 Qt 自带的中文翻译，让标准按钮显示为「是 / 否」「确定 / 取消」。"""
    try:
        translator = QTranslator()
        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if translator.load("qt_zh_CN", path):
            app.installTranslator(translator)
            # 交给 app 持有，避免被回收后翻译失效
            app._zqapi_translator = translator  # type: ignore[attr-defined]
    except Exception:
        pass


def _apply_startup_palette(app: QApplication) -> None:
    try:
        palette = app.palette()
        background = QColor(MAIN_WINDOW_BACKGROUND)
        palette.setColor(QPalette.ColorRole.Window, background)
        palette.setColor(QPalette.ColorRole.Base, background)
        app.setPalette(palette)
    except Exception:
        pass


def _quit(code: int) -> int:
    """收尾：刷新缓冲并安全退出，保障 SQLite WAL 与文件流正常落盘。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:  # pragma: no cover
            pass
    try:
        import logging

        logging.shutdown()
    except Exception:  # pragma: no cover
        pass

    # 若为打包的 frozen 环境，启动一个 1.5s 兜底守护，避免极罕见的底层 DLL 挂住进程
    if getattr(sys, "frozen", False):
        import threading
        def _fallback_kill():
            import time
            time.sleep(1.5)
            os._exit(int(code))
        t = threading.Thread(target=_fallback_kill, daemon=True)
        t.start()

    return int(code)


def _run_self_check(app: QApplication, window: ToolboxWindow) -> int:
    """构建界面并抓两张标签页截图，用于打包后的启动验证（跑完事件循环再返回退出码）。"""
    from pathlib import Path

    out_dir = get_app_dir() / "self-check"
    out_dir.mkdir(parents=True, exist_ok=True)
    codes = [0]
    # 按标签名对应文件名，标签顺序变了也不用改这里
    file_names = {"朱雀检测": "tab-zhuque.png", "文本比较": "tab-compare.png", "帮助": "tab-help.png"}

    def grab(index: int, name: str) -> None:
        window.tabs.setCurrentIndex(index)
        QApplication.processEvents()
        ok = window.grab().save(str(Path(out_dir) / name))
        print("self-check: %s -> %s" % (name, "ok" if ok else "failed"))
        if not ok:
            codes[0] = 1

    def run() -> None:
        for index in range(window.tabs.count()):
            label = window.tabs.tabText(index)
            grab(index, file_names.get(label, "tab-%d.png" % index))
        window.close()
        app.quit()

    QTimer.singleShot(600, run)
    app.exec()
    return codes[0]


def _run_startup_probe(app: QApplication, window: ToolboxWindow) -> int:
    """走**正常的启动路径**（show + exec），把窗口自身的状态写进 json 后退出。

    外部工具看窗口有没有显示，会被远程会话/最小化状态干扰；这里让程序自己回答：
    窗口是否可见、是否被最小化、尺寸多少、当前在哪个标签页。
    """
    import json

    target = get_app_dir() / "startup-probe.json"

    def report() -> None:
        screen = window.screen()
        geometry = None
        if screen is not None:
            available = screen.availableGeometry()
            geometry = [available.width(), available.height()]
        payload = {
            "title": window.windowTitle(),
            "visible": bool(window.isVisible()),
            "minimized": bool(window.isMinimized()),
            "maximized": bool(window.isMaximized()),
            "window_state": int(window.windowState().value),
            "size": [window.width(), window.height()],
            "position": [window.x(), window.y()],
            "device_pixel_ratio": round(float(window.devicePixelRatioF()), 4),
            "screen_available": geometry,
            "tabs": [window.tabs.tabText(i) for i in range(window.tabs.count())],
            "current_tab": window.tabs.tabText(window.tabs.currentIndex()),
            "app_dir": str(get_app_dir()),
        }
        try:
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print("startup-probe:", json.dumps(payload, ensure_ascii=False))
        except OSError as exc:  # pragma: no cover - 目录只读
            print("startup-probe 写入失败：%s" % exc)
        window.close()
        app.quit()

    QTimer.singleShot(1500, report)
    app.exec()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ZqApiToolbox")
        except Exception:
            pass

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("AI写作工具箱")
    app.setOrganizationName("ZqApi")
    _install_chinese_translations(app)
    _apply_startup_palette(app)
    app.setWindowIcon(app_icon())

    window = ToolboxWindow()
    app.aboutToQuit.connect(window.cleanup)
    window.show()

    if "--self-check" in args:
        return _quit(_run_self_check(app, window))
    if "--startup-probe" in args:
        return _quit(_run_startup_probe(app, window))

    return _quit(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
