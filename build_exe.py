"""把本工具打包成一个独立的 Windows exe。

用法（在 zqapi 目录下）::

    python build_exe.py            # 单文件 exe -> dist/ZqApi.exe
    python build_exe.py --onedir   # 目录版（启动更快，便于排查缺文件）
    python build_exe.py --console  # 保留控制台窗口（排查崩溃用）

打包用的是 PyInstaller，界面是 PyQt6，除 Qt 自身外没有第三方运行时依赖。
仅需 ``assets/`` 目录随包附带（通过 ``--add-data`` 注入，运行时从 ``sys._MEIPASS`` 读取）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
APP_NAME = "ZqApi"
ENTRY = HERE / "main.py"
ASSETS = HERE / "assets"
DIST = HERE / "dist"
WORK = HERE / "build"


def _check_environment() -> bool:
    try:
        import PyQt6  # noqa: F401
    except ImportError:
        print("缺少 PyQt6，请先执行：pip install -r requirements.txt", file=sys.stderr)
        return False
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("缺少 PyInstaller，请先执行：pip install -r requirements.txt", file=sys.stderr)
        return False
    print("PyInstaller %s" % result.stdout.strip())
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    onedir = "--onedir" in args
    console = "--console" in args

    if not _check_environment():
        return 1
    if not ASSETS.is_dir():
        print("找不到素材目录：%s" % ASSETS, file=sys.stderr)
        return 1

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir" if onedir else "--onefile",
        "--console" if console else "--windowed",
        "--name",
        APP_NAME,
        "--icon",
        str(ASSETS / "app.ico"),
        # 图标等只读素材：源目录 assets -> 包内 assets
        "--add-data",
        "%s%sassets" % (ASSETS, ";" if sys.platform == "win32" else ":"),
        # 这些 Qt 模块用不到，显式排除，能省下几十 MB
        "--exclude-module",
        "PyQt6.QtWebEngineCore",
        "--exclude-module",
        "PyQt6.QtQml",
        "--exclude-module",
        "PyQt6.QtQuick",
        "--exclude-module",
        "PyQt6.QtMultimedia",
        "--exclude-module",
        "PyQt6.QtPdf",
        "--exclude-module",
        "PyQt6.QtSql",
        "--exclude-module",
        "PyQt6.QtTest",
        "--exclude-module",
        "PyQt6.QtOpenGL",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        "--specpath",
        str(WORK),
        str(ENTRY),
    ]
    print("执行：%s" % " ".join(command))
    result = subprocess.run(command, cwd=str(HERE))
    if result.returncode != 0:
        print("打包失败。", file=sys.stderr)
        return result.returncode

    target = DIST / (APP_NAME + ".exe" if not onedir else APP_NAME)
    if not target.exists():
        print("打包结束但没找到产物：%s" % target, file=sys.stderr)
        return 1
    size_mb = target.stat().st_size / 1024 / 1024 if target.is_file() else 0
    print("\n打包完成：%s%s" % (target, ("（%.1f MB）" % size_mb) if size_mb else ""))
    print("自检：%s --self-check" % target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
