#!/usr/bin/env python3
"""DY 桌面启动器 — 内嵌 WebUI 的原生窗口应用。

需要: pip install pywebview

用法:
    python launcher.py                 # 桌面窗口模式
    python launcher.py --server-only   # 仅启动 Flask（不弹窗口）
"""

import os
import sys
import threading
import argparse
import logging

# 确保项目根目录在 Python 搜索路径中
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# 抑制 Flask/Werkzeug 的启动日志
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════
# 全局资源（用于窗口关闭时清理）
# ═══════════════════════════════════════════════════════════════
_scheduler = None
_db = None
_task_manager = None
_server_thread = None


def start_server(host: str = "127.0.0.1", port: int = 5050) -> threading.Thread:
    """在后台线程启动 Flask 服务器。"""
    from webui import app, db, _sync_scheduler, _task_manager

    global _scheduler, _db, _task_manager

    _db = db
    _task_manager = _task_manager

    # 数据迁移 + 启动调度器（同 webui.main() 逻辑）
    from webui import get_download_dir
    download_dir = get_download_dir()
    if db.count_files() == 0:
        migrated = db.migrate_from_metadata_json(download_dir)
        if migrated:
            print(f"📦 从 metadata.json 迁移了 {migrated} 个帖子到 SQLite")

    # 配置调度器（如果未在模块级别启动的话）
    if _sync_scheduler:
        _scheduler = _sync_scheduler
    else:
        from douyin.scheduler import SyncScheduler
        from webui import _make_session, DOWNLOAD_DIR
        _scheduler = SyncScheduler(
            db=db,
            download_dir=str(DOWNLOAD_DIR),
            session_factory=_make_session,
        )
        _scheduler.start(interval_hours=6)

    def _run():
        app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)

    thread = threading.Thread(target=_run, daemon=True, name="flask-server")
    thread.start()
    return thread


def stop_server():
    """优雅关闭所有后台服务。"""
    if _scheduler:
        try:
            _scheduler.stop()
        except Exception:
            pass
    if _task_manager:
        try:
            _task_manager.shutdown(wait=False)
        except Exception:
            pass
    if _db:
        try:
            _db.close()
        except Exception:
            pass


def launch_window(url: str = "http://127.0.0.1:5050",
                  title: str = "DY - 抖音下载工具",
                  width: int = 1200,
                  height: int = 800):
    """在原生窗口中打开 WebUI。"""
    import webview

    # macOS: 确保创建的是标准桌面窗口
    window = webview.create_window(
        title=title,
        url=url,
        width=width,
        height=height,
        min_size=(800, 600),
        resizable=True,
        confirm_close=True,  # 关闭前确认
        text_select=True,    # 允许选中文本
    )

    webview.start(gui="cocoa" if sys.platform == "darwin" else None)

    # 窗口关闭后：清理
    stop_server()


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="DY 桌面启动器")
    parser.add_argument("--server-only", action="store_true",
                        help="仅启动 Flask 服务器，不弹窗口")
    parser.add_argument("--host", default="127.0.0.1",
                        help="服务器绑定地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=5050,
                        help="服务器端口（默认 5050）")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器（同 --server-only）")
    args = parser.parse_args()

    print("=" * 50)
    print("🖥  DY - 抖音下载工具")
    print("=" * 50)

    # 启动服务器
    url = f"http://{args.host}:{args.port}"
    if args.host == "0.0.0.0":
        url = f"http://127.0.0.1:{args.port}"

    global _server_thread
    _server_thread = start_server(host=args.host, port=args.port)
    print(f"   WebUI: {url}")

    if args.server_only or args.no_browser:
        print("   按 Ctrl+C 停止服务器")
        try:
            # 保持主线程存活
            while True:
                _server_thread.join(1)
                if not _server_thread.is_alive():
                    break
        except KeyboardInterrupt:
            print("\n⏹ 正在关闭...")
        finally:
            stop_server()
    else:
        print("   启动桌面窗口...")
        launch_window(url)


if __name__ == "__main__":
    main()
