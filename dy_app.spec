# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格文件 — DY 抖音下载工具。

用法:
    pyinstaller dy_app.spec

输出:
    macOS:  dist/DY抖音下载.app
    Windows: dist/DY抖音下载.exe
"""

import sys, os
from pathlib import Path

PROJECT_DIR = Path(SPEC).parent  # noqa: F821

# ── 隐藏导入 ──
hiddenimports = [
    "flask", "flask.json",
    "werkzeug", "jinja2", "markupsafe", "itsdangerous", "click",
    "douyin", "douyin.encrypt", "douyin.session", "douyin.user",
    "douyin.downloader", "douyin.live", "douyin.database",
    "douyin.scheduler", "douyin.anti_crawl", "douyin.task_manager",
    "douyin.utils",
    "gmssl", "apscheduler",
    "apscheduler.schedulers", "apscheduler.schedulers.background",
    "apscheduler.triggers", "apscheduler.triggers.interval",
    "requests", "urllib3",
    "tzlocal", "bottle", "webview",
]

# ── 数据文件 ──
datas = [
    (str(PROJECT_DIR / "templates"), "templates"),
    (str(PROJECT_DIR / "config.json"), "."),
]

# ── 排除模块 ──
excludes = [
    "PyQt5", "PySide2", "PySide6", "matplotlib",
    "tkinter", "unittest", "pdb",
]

# ── macOS 设置 ──
if sys.platform == "darwin":
    info_plist = {
        "CFBundleName": "DY抖音下载",
        "CFBundleDisplayName": "DY - 抖音下载工具",
        "CFBundleIdentifier": "com.dy.downloader",
        "CFBundleVersion": "2.0.0",
        "CFBundleShortVersionString": "2.0.0",
        "NSHighResolutionCapable": True,
    }
    console_flag = False
else:
    info_plist = {}
    console_flag = False

a = Analysis(
    [str(PROJECT_DIR / "launcher.py")],
    pathex=[str(PROJECT_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DY抖音下载",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=console_flag,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# COLLECT: 收集所有文件到输出目录
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DY抖音下载",
)

if sys.platform == "darwin":
    # macOS: COLLECT 目录 → .app bundle
    app = BUNDLE(
        coll,
        name="DY抖音下载.app",
        icon=None,
        bundle_identifier="com.dy.downloader",
        info_plist=info_plist,
    )
# Windows: COLLECT 目录即最终产物（dist/DY抖音下载/DY抖音下载.exe）
