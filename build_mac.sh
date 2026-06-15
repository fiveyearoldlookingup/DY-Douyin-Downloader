#!/bin/bash
# ============================================================
# DY 抖音下载工具 — macOS 打包脚本
# ============================================================
# 输出: dist/DY抖音下载.app (~33MB)
#
# 前置要求:
#   pip install -r requirements.txt
# ============================================================

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================"
echo "  🍎 DY macOS 打包"
echo "============================================"

# 清理
echo "🧹 清理..."
rm -rf build dist
xattr -cr . 2>/dev/null || true

# 检查依赖
python3 -c "import pywebview, PyInstaller" 2>/dev/null || {
    echo "📦 安装依赖..."
    pip install -r requirements.txt
}

# 打包
echo "📦 PyInstaller 打包..."
python3 -m PyInstaller dy_app.spec

# 清理扩展属性 + 签名
echo "🔐 代码签名..."
rm -rf dist/DY抖音下载 2>/dev/null || true  # 移除中间目录，只保留 .app
xattr -cr dist/DY抖音下载.app 2>/dev/null || true
codesign -s - --force --deep dist/DY抖音下载.app 2>/dev/null || true

# 输出
echo ""
echo "============================================"
APP_SIZE=$(du -sh dist/DY抖音下载.app | cut -f1)
echo "  ✅ 打包成功: dist/DY抖音下载.app ($APP_SIZE)"
echo "============================================"
echo ""
echo "打开方式: open dist/DY抖音下载.app"
