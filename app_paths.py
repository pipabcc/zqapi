"""本工具的目录解析（复制自 deepcat/utils/paths.py，去掉对主工程布局的依赖）。

- 打包成 exe 后：``<exe 所在目录>/``
- 源码运行：本文件所在目录（``zqapi/``）
"""

from __future__ import annotations

import sys
from pathlib import Path


def get_app_dir() -> Path:
    """程序根目录：所有可写数据都放它下面。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_dir() -> Path:
    """只读资源根目录（PyInstaller onefile 解包后是 ``sys._MEIPASS``）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent


def get_assets_dir() -> Path:
    return get_resource_dir() / "assets"


def get_data_dir() -> Path:
    """可写数据目录（历史库、配置都在这里），首次访问时创建。"""
    directory = get_app_dir() / "data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory
