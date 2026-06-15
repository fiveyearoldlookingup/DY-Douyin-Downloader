@echo off
REM ============================================================
REM DY 抖音下载工具 — Windows 打包脚本
REM ============================================================
REM 输出: dist\DY抖音下载\DY抖音下载.exe
REM
REM 前置要求:
REM   pip install -r requirements.txt
REM   Microsoft Edge WebView2 运行时（Win10/11 自带）
REM ============================================================

setlocal enabledelayedexpansion

echo ============================================
echo   DY Windows 打包
echo ============================================
echo.

REM 清理
echo [1/3] 清理...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM 检查依赖
echo [2/3] 检查依赖...
python -c "import pywebview, PyInstaller" 2>nul || (
    echo 安装依赖...
    pip install -r requirements.txt
)

REM 打包
echo [3/3] 打包...
python -m PyInstaller dy_app.spec

REM 验证
if exist "dist\DY抖音下载\DY抖音下载.exe" (
    echo.
    echo ============================================
    echo   ✅ 打包成功！
    echo   dist\DY抖音下载\DY抖音下载.exe
    echo ============================================
    echo.
    echo 💡 分发方式:
    echo    1. 直接压缩 dist\DY抖音下载 文件夹发给用户
    echo    2. 或使用 Inno Setup / NSIS 制作安装包
) else (
    echo ❌ 打包失败！
    pause
)
